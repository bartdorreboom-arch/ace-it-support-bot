"""
ACE IT Support Bot
------------------
A real-time Slack bot that:
- Listens to #ace-it-support for new questions AND follow-up questions in threads
- Searches Notion KB for relevant existing articles before answering
- Combines KB knowledge with Claude AI for the best possible answer
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

# ── Main: handle all messages (new questions + thread follow-ups) ──────────
@app.event("message")
def handle_message(event, client):
    # Skip bot messages, edits, deletes
    if event.get("bot_id"):
        return
    if event.get("subtype") in ("message_changed", "message_deleted", "bot_message"):
        return

    user_id    = event.get("user", "")
    text       = event.get("text", "").strip()
    channel_id = event["channel"]

    if not text or user_id == BOT_USER_ID:
        return

    is_thread_reply = event.get("thread_ts") and event["thread_ts"] != event["ts"]

    if is_thread_reply:
        # Follow-up question in an existing thread
        thread_ts = event["thread_ts"]

        # Fetch thread history so Claude has full context
        thread = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts
        )
        thread_messages = thread.get("messages", [])

        # Only respond if the bot was involved in this thread before
        bot_was_here = any(m.get("user") == BOT_USER_ID for m in thread_messages)
        if not bot_was_here:
            return

        log.info(f"Follow-up question from {user_id} in thread {thread_ts}: {text[:80]}...")

        # Build conversation history for Claude
        history = build_thread_history(thread_messages)

        # Search KB for context
        kb_context = search_notion_kb(text)

        # Answer with full thread context
        answer = ask_claude_with_history(
            question=text,
            history=history,
            kb_context=kb_context,
            user_id=user_id
        )

        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=answer
        )
        log.info(f"Replied to follow-up in thread {thread_ts}")

    else:
        # New top-level question
        thread_ts = event["ts"]
        log.info(f"New question from {user_id}: {text[:80]}...")

        # Search KB for relevant existing articles
        kb_context = search_notion_kb(text)
        if kb_context:
            log.info("Found relevant KB articles, combining with AI answer")
        else:
            log.info("No relevant KB articles found, answering from scratch")

        # Generate answer combining KB + Claude
        answer = ask_claude(question=text, kb_context=kb_context, user_id=user_id)

        # Post reply in thread
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=answer
        )
        log.info(f"Replied to thread {thread_ts}")

        # Always create a KB article automatically
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


# ── Build thread history for follow-up context ─────────────────────────────
def build_thread_history(messages: list) -> list:
    """Convert Slack thread messages to Claude conversation format."""
    history = []
    for msg in messages:
        msg_user = msg.get("user", "")
        msg_text = msg.get("text", "").strip()
        if not msg_text:
            continue
        if msg_user == BOT_USER_ID:
            history.append({"role": "assistant", "content": msg_text})
        else:
            history.append({"role": "user", "content": msg_text})
    return history


# ── Claude: answer a new question, combining KB + AI ──────────────────────
def ask_claude(question: str, kb_context: str, user_id: str) -> str:
    system_prompt = """Je bent een vriendelijke en kundige IT-supportbot voor ACE, een creatief bureau in Nederland.
Je beantwoordt vragen van collega's in #ace-it-support.

Richtlijnen:
- Antwoord altijd in dezelfde taal als de vraag (Nederlands of Engels)
- Als er relevante kennisbank artikelen beschikbaar zijn, gebruik die dan als basis voor je antwoord en combineer ze met je eigen kennis
- Wees beknopt maar volledig — geen onnodige uitweidingen
- Geef concrete stappen als dat relevant is
- Als je het antwoord niet weet, zeg dat eerlijk en stel voor om contact op te nemen met IT via IT@ace.nl
- Toon empathie — IT-problemen zijn frustrerend"""

    user_prompt = f"""Relevante kennisbank artikelen (gebruik deze als basis voor je antwoord):
{kb_context if kb_context else "Geen relevante artikelen gevonden — beantwoord op basis van je eigen kennis."}

Vraag van <@{user_id}>:
{question}"""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    return response.content[0].text


# ── Claude: answer a follow-up question with full thread history ───────────
def ask_claude_with_history(question: str, history: list, kb_context: str, user_id: str) -> str:
    system_prompt = """Je bent een vriendelijke en kundige IT-supportbot voor ACE, een creatief bureau in Nederland.
Je beantwoordt vragen van collega's in #ace-it-support.

Je hebt de volledige gespreksgeschiedenis van de thread beschikbaar. Gebruik die context om de vervolgvraag goed te beantwoorden.

Richtlijnen:
- Antwoord altijd in dezelfde taal als de vraag (Nederlands of Engels)
- Gebruik de gespreksgeschiedenis als context — herhaal niet wat al gezegd is
- Als er relevante kennisbank artikelen zijn, gebruik die dan als aanvulling
- Wees beknopt maar volledig
- Als je het antwoord niet weet, verwijs naar IT@ace.nl"""

    # Add KB context to the last user message if available
    final_question = question
    if kb_context:
        final_question = f"{question}\n\n[Relevante KB artikelen: {kb_context[:500]}]"

    # Replace last user message with enriched version
    messages = history[:-1] + [{"role": "user", "content": final_question}]

    # Ensure conversation starts with user message
    if not messages or messages[0]["role"] != "user":
        messages = [{"role": "user", "content": final_question}]

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=messages
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
            # Only include pages from our KB database
            parent = page.get("parent", {})
            if parent.get("database_id", "").replace("-", "") != NOTION_DB_ID.replace("-", ""):
                continue

            title_prop = page.get("properties", {}).get("Naam", {}) or page.get("properties", {}).get("title", {})
            title_items = title_prop.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_items) if title_items else "Geen titel"

            # Get page content
            blocks = notion.blocks.children.list(block_id=page["id"], page_size=10)
            content_parts = []
            for block in blocks.get("results", []):
                block_type = block.get("type")
                rich_text = block.get(block_type, {}).get("rich_text", [])
                content_parts.append("".join(t.get("plain_text", "") for t in rich_text))

            content = " ".join(content_parts)[:800]
            if title or content:
                articles.append(f"**{title}**\n{content}")

        return "\n\n---\n\n".join(articles) if articles else ""
    except Exception as e:
        log.warning(f"Notion search failed: {e}")
        return ""


# ── Notion: create a KB article from question + answer ────────────────────
def create_notion_article(question: str, answer: str, user_id: str) -> str:
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

    title = question[:80] if len(question) <= 80 else question[:77] + "..."

    page = notion.pages.create(
        parent={"database_id": NOTION_DB_ID},
        properties={
            "Naam": {
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
