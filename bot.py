"""
ACE IT Support Bot
------------------
A real-time Slack bot that:
- Listens to #ace-it-support for new questions
- Answers them using Claude AI + Notion knowledge base
- DMs Bart when a thread is resolved to ask about saving a KB article
- Creates Notion KB articles on approval

Requirements: pip install slack_bolt anthropic notion-client
"""

import os
import json
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
CHANNEL_NAME   = "ace-it-support"
BART_USER_ID   = "U09LUJUF7PG"          # Bart Dorreboom — IT admin
NOTION_DB_ID   = os.environ["NOTION_DB_ID"]
BOT_USER_ID    = None                   # Set automatically on startup

# Track threads we've asked Bart about (thread_ts → thread summary)
pending_kb: dict[str, dict] = {}

# ── Startup: get bot's own user ID so we can ignore our own messages ───────
@app.event("app_mention")
def handle_mention(event, say):
    say("👋 IT Support Bot is online!", thread_ts=event["ts"])

def get_bot_user_id():
    global BOT_USER_ID
    result = app.client.auth_test()
    BOT_USER_ID = result["user_id"]
    log.info(f"Bot user ID: {BOT_USER_ID}")

# ── Main: handle new channel messages ──────────────────────────────────────
@app.event("message")
def handle_message(event, client):
    # Skip: bot messages, edited messages, deleted messages, thread replies
    if event.get("bot_id"):
        return
    if event.get("subtype") in ("message_changed", "message_deleted", "bot_message"):
        return
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        # This is a reply in a thread — check if it resolves the thread
        handle_thread_reply(event, client)
        return

    channel_id = event["channel"]
    thread_ts  = event["ts"]
    user_id    = event.get("user", "")
    text       = event.get("text", "").strip()

    if not text or user_id == BOT_USER_ID:
        return

    log.info(f"New question from {user_id}: {text[:80]}...")

    # 1. Search Notion KB for relevant context
    kb_context = search_notion_kb(text)

    # 2. Generate answer with Claude
    answer = ask_claude(question=text, kb_context=kb_context, user_id=user_id)

    # 3. Post reply in thread
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=answer
    )
    log.info(f"Replied to thread {thread_ts}")


def handle_thread_reply(event, client):
    """Check if a thread reply looks like a resolution, and DM Bart if so."""
    text    = event.get("text", "").lower()
    user_id = event.get("user", "")

    # Skip bot replies
    if user_id == BOT_USER_ID:
        return

    # Resolution signals (Dutch + English)
    resolved_signals = [
        "dank", "thanks", "bedankt", "top", "gelukt", "werkt", "fixed",
        "opgelost", "perfect", "thanks!", "great", "goed zo", "super",
        "gevonden", "dankjewel", "awesome"
    ]

    if any(signal in text for signal in resolved_signals):
        thread_ts  = event["thread_ts"]
        channel_id = event["channel"]

        # Don't ask twice for the same thread
        if thread_ts in pending_kb:
            return

        # Fetch the thread to build a summary
        thread = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts
        )
        messages = thread.get("messages", [])
        original_question = messages[0].get("text", "") if messages else "(onbekend)"

        # Store for later when Bart says yes
        pending_kb[thread_ts] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "question": original_question,
            "messages": messages,
        }

        # DM Bart
        client.chat_postMessage(
            channel=BART_USER_ID,
            text=(
                f"✅ Het lijkt erop dat een vraag in *#ace-it-support* is opgelost!\n\n"
                f"*Vraag:* {original_question[:200]}{'...' if len(original_question) > 200 else ''}\n\n"
                f"Zal ik dit opslaan als een kennisbankartikel in Notion zodat collega's het makkelijk kunnen vinden? "
                f"Antwoord met *ja* of *nee* (en voeg de thread-id toe: `{thread_ts}`)."
            )
        )
        log.info(f"Sent KB prompt to Bart for thread {thread_ts}")


# ── DM handler: Bart replies yes/no ───────────────────────────────────────
@app.event("message")
def handle_dm(event, client):
    # Only listen to DMs from Bart
    if event.get("channel_type") != "im":
        return
    if event.get("user") != BART_USER_ID:
        return
    if event.get("bot_id"):
        return

    text = event.get("text", "").lower()

    # Find which thread Bart is approving — he includes the thread_ts
    matched_thread = None
    for thread_ts in pending_kb:
        if thread_ts in event.get("text", ""):
            matched_thread = thread_ts
            break

    # If only one pending thread, assume it's that one
    if not matched_thread and len(pending_kb) == 1:
        matched_thread = list(pending_kb.keys())[0]

    if not matched_thread:
        client.chat_postMessage(
            channel=BART_USER_ID,
            text="Hmm, ik kan niet bepalen over welke thread je het hebt. Kun je het thread-id meesturen?"
        )
        return

    if any(w in text for w in ["ja", "yes", "yep", "jep", "ok", "oke", "sure"]):
        thread_data = pending_kb.pop(matched_thread)
        notion_url = create_notion_article(thread_data)
        client.chat_postMessage(
            channel=BART_USER_ID,
            text=f"📚 Kennisbankartikel aangemaakt! {notion_url}"
        )
        # Also confirm in the original Slack thread
        client.chat_postMessage(
            channel=thread_data["channel_id"],
            thread_ts=thread_data["thread_ts"],
            text=f"📚 Deze oplossing is opgeslagen in onze kennisbank: {notion_url}"
        )
        log.info(f"Created Notion KB article for thread {matched_thread}")

    elif any(w in text for w in ["nee", "no", "nope", "niet", "skip"]):
        pending_kb.pop(matched_thread, None)
        client.chat_postMessage(
            channel=BART_USER_ID,
            text="👍 Geen probleem, ik sla het niet op."
        )
        log.info(f"Bart declined KB article for thread {matched_thread}")


# ── Claude: generate an IT support answer ─────────────────────────────────
def ask_claude(question: str, kb_context: str, user_id: str) -> str:
    system_prompt = """Je bent een vriendelijke en kundige IT-supportbot voor ACE, een creatief bureau in Nederland.
Je beantwoordt vragen van collega's in #ace-it-support.

Richtlijnen:
- Antwoord altijd in dezelfde taal als de vraag (Nederlands of Engels)
- Wees beknopt maar volledig — geen onnodige uitweidingen
- Geef concrete stappen als dat relevant is
- Als je het antwoord niet weet, zeg dat eerlijk en stel voor om contact op te nemen met IT via IT@ace.nl
- Verwijs naar bestaande KB-artikelen als die beschikbaar zijn
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


# ── Notion: create a KB article ────────────────────────────────────────────
def create_notion_article(thread_data: dict) -> str:
    question   = thread_data["question"]
    messages   = thread_data["messages"]

    # Build a clean summary with Claude
    conversation = "\n".join(
        f"{m.get('user', 'bot')}: {m.get('text', '')}"
        for m in messages
    )
    summary_prompt = f"""Maak een beknopt kennisbankartikel op basis van dit Slack-gesprek.

Gesprek:
{conversation}

Gebruik exact dit formaat:

## Probleem
[1-2 zinnen: wat was het probleem?]

## Oplossing
[Duidelijke stappen of uitleg]

## Notities
[Optioneel: uitzonderingen, varianten, gerelateerde issues]"""

    summary_response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": summary_prompt}]
    )
    article_body = summary_response.content[0].text

    # Create title
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

    page_url = page.get("url", "https://notion.so")
    log.info(f"Notion article created: {page_url}")
    return page_url


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    get_bot_user_id()
    log.info("Starting ACE IT Support Bot in Socket Mode...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
