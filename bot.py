"""
ACE IT Support Bot
------------------
A real-time Slack bot that:
- Listens to #ace-it-support for new questions
- Searches Notion KB for relevant existing articles before answering
- Answers questions using Claude AI
- Automatically creates a Notion KB article after every answered question

Requirements: pip install slack_bolt anthropic notion-client
"""

import os
import logging
from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from notion_client import Client as NotionClient

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Clients ────────────────────────────────────────────────────────────────
app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic()
notion = NotionClient(auth=os.environ["NOTION_API_KEY"])

# ── Config ─────────────────────────────────────────────────────────────────
BART_USER_ID = "U09LUJUF7PG"   # Bart Dorreboom — IT admin
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
BOT_USER_ID  = None             # Set automatically on startup

# ── Startup: get bot's own user ID so we can ignore our own messages ───────
def get_bot_user_id():
    global BOT_USER_ID
    result = app.client.auth_test()
    BOT_USER_ID = result["user_id"]
    log.info(f"Bot user ID: {BOT_USER_ID}")

# ── Main: handle new channel messages ──────────────────────────────────────
@app.event("message")
def handle_message(event, client):
    # Skip bot messages, edits, deletes, and thread replies
    if event.get("bot_id"):
        return
    if event.get("subtype") in ("message_changed", "message_deleted", "bot_message"):
        return
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        return  # Ignore replies — only respond to new top-level questions

    channel_id = event["channel"]
    thread_ts  = event["ts"]
    user_id    = event.get("user", "")
    text       = event.get("text", "").strip()

    if not text or user_id == BOT_USER_ID:
        return

    log.info(f"New question from {user_id}: {text[:80]}...")

    # 1. Search Notion KB for relevant existing articles
    kb_context = search_notion_kb(text)
    if kb_context:
        log.info("Found relevant KB articles, using as context")
    else:
        log.info("No relevant KB articles found, answering from scratch")

    # 2. Generate answer with Claude
    answer = ask_claude(question=text, kb_context=kb_context, user_id=user_id)

    # 3. Post reply in thread
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=answer
    )
    log.info(f"Replied to thread {thread_ts}")

    # 4. Always create a KB article automatically
    try:
        notion_url = create_notion_article(
            question=text,
            answer=answer,
            user_id=user_id
        )
        log.info(f"KB article created: {notion_url}")

        # Notify Bart via DM so he stays informed and can correct if needed
        client.chat_postMessage(
            channel=BART_USER_ID,
            text=(
                f"📚 Nieuw kennisbankartikel aangemaakt op basis van een vraag in *#ace-it-support*\n\n"
                f"*Vraag:* {text[:200]}{'...' if len(text) > 200 else ''}\n\n"
                f"*Artikel:* {notion_url}\n\n"
                f"_Pas het artikel aan als het antwoord niet klopt._"
            )
        )
    except Exception as e:
        log.error(f"Failed to create KB article: {e}")


# ── Claude: generate an IT support answer ─────────────────────────────────
def ask_claude(question: str, kb_context: str, user_id: str) -> str:
    system_prompt = """Je bent een vriendelijke en kundige IT-supportbot voor ACE, een creatief bureau in Nederland.
Je beantwoordt vragen van collega's in #ace-it-support.

Richtlijnen:
- Antwoord altijd in dezelfde taal als de vraag (Nederlands of Engels)
- Wees beknopt maar volledig — geen onnodige uitweidingen
- Geef concrete stappen als dat relevant is
- Als je het antwoord niet weet, zeg dat eerlijk en stel voor om contact op te nemen met IT via IT@ace.nl
- Als er relevante KB-artikelen zijn, baseer je antwoord daarop
- Toon empathie — IT-problemen zijn frustrerend"""

    user_prompt = f"""Relevante kennisbank artikelen:
{kb_context if kb_context else "Geen relevante artikelen gevonden."}

Vraag van <@{user_id}>:
{question}"""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    return response.content[0].text


# ── Notion: search KB for relevant articles ────────────────────────────────
def search_notion_kb(query: str) -> str:
    try:
        results = notion.search(
            query=query,
            filter={"value": "page", "property": "object"},
            page_size=3
        )
        articles = []
        for page in results.get("results", []):
            title_prop = page.get("properties", {}).get("title", {})
            title_items = title_prop.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_items) if title_items else "Geen titel"

            # Get page content (first 500 chars)
            blocks = notion.blocks.children.list(block_id=page["id"], page_size=5)
            content_parts = []
            for block in blocks.get("results", []):
                block_type = block.get("type")
                rich_text = block.get(block_type, {}).get("rich_text", [])
                content_parts.append("".join(t.get("plain_text", "") for t in rich_text))

            content = " ".join(content_parts)[:500]
            articles.append(f"**{title}**\n{content}")

        return "\n\n---\n\n".join(articles) if articles else ""
    except Exception as e:
        log.warning(f"Notion search failed: {e}")
        return ""


# ── Notion: create a KB article from question + answer ────────────────────
def create_notion_article(question: str, answer: str, user_id: str) -> str:
    # Ask Claude to write a clean KB article based on the Q&A
    summary_prompt = f"""Maak een beknopt kennisbankartikel op basis van deze vraag en het antwoord.

Vraag: {question}
Antwoord: {answer}

Gebruik exact dit formaat (schrijf alleen de inhoud, geen extra uitleg):

## Probleem
[1-2 zinnen: wat was het probleem of de vraag?]

## Oplossing
[Duidelijke stappen of uitleg, gebaseerd op het antwoord]

## Notities
[Optioneel: uitzonderingen, varianten, of gerelateerde tips]"""

    summary_response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": summary_prompt}]
    )
    article_body = summary_response.content[0].text

    # Use the question as the title (truncated)
    title = question[:80] if len(question) <= 80 else question[:77] + "..."

    # Create page in Notion
    page = notion.pages.create(
        parent={"database_id": NOTION_DB_ID},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": article_body}}]
                }
            }
        ]
    )

    return page.get("url", "https://notion.so")


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    get_bot_user_id()
    log.info("Starting ACE IT Support Bot in Socket Mode...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
