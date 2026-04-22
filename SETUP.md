# ACE IT Support Bot — Setup Guide

Estimated time: ~45 minutes

---

## Step 1: Create a Slack App (10 min)

1. Go to https://api.slack.com/apps and click **Create New App → From scratch**
2. Name it `ACE IT Support Bot`, select your ACE workspace
3. In the left sidebar, go to **Socket Mode** → Enable it → Create an App-Level Token with `connections:write` scope → copy the token (starts with `xapp-`)
4. Go to **OAuth & Permissions** → add these Bot Token Scopes:
   - `channels:history`
   - `channels:read`
   - `chat:write`
   - `im:history`
   - `im:write`
   - `users:read`
5. Go to **Event Subscriptions** → Enable Events → Subscribe to bot events:
   - `message.channels`
   - `message.im`
6. Go to **Install App** → Install to Workspace → copy the Bot User OAuth Token (starts with `xoxb-`)
7. Note your **Signing Secret** from Basic Information

---

## Step 2: Create a Notion Integration (10 min)

1. Go to https://www.notion.so/my-integrations → **New integration**
2. Name it `ACE IT Bot`, select your workspace, click Submit
3. Copy the **Internal Integration Token** (starts with `secret_`)
4. Create a new Notion database called `IT Knowledge Base` (or use an existing one)
5. Open the database → click `...` → **Add connections** → select your integration
6. Copy the database ID from the URL:
   - URL looks like: `https://notion.so/your-workspace/`**`abc123def456...`**`?v=...`
   - The long ID before the `?` is your database ID

---

## Step 3: Get your Anthropic API key (5 min)

1. Go to https://console.anthropic.com
2. Click **API Keys** → Create Key
3. Copy the key (starts with `sk-ant-`)

---

## Step 4: Deploy to Railway (15 min)

1. Push this folder to a GitHub repository (public or private)
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**
3. Select your repository
4. Go to your project **Variables** tab and add:

```
SLACK_BOT_TOKEN     = xoxb-...
SLACK_APP_TOKEN     = xapp-...
ANTHROPIC_API_KEY   = sk-ant-...
NOTION_API_KEY      = secret_...
NOTION_DB_ID        = abc123...
```

5. Railway will auto-deploy. Check the **Logs** tab — you should see:
   ```
   Starting ACE IT Support Bot in Socket Mode...
   ```

---

## Step 5: Invite the bot to your channel (2 min)

In Slack, go to `#ace-it-support` and type:
```
/invite @ACE IT Support Bot
```

That's it! The bot is now live and listening. 🎉

---

## How it works in practice

| Situation | What happens |
|---|---|
| Someone asks a question | Bot replies in-thread within ~3 seconds |
| Someone says "thanks" / "werkt" / "top" | Bot DMs Bart: "Save to KB?" |
| Bart replies "ja" | Bot creates Notion article + posts link in thread |
| Bart replies "nee" | Bot acknowledges, moves on |

---

## Troubleshooting

**Bot doesn't respond?**
- Check Railway logs for errors
- Make sure the bot is invited to `#ace-it-support`
- Verify all 5 environment variables are set correctly

**Notion articles not creating?**
- Check that the Notion integration is shared with the database
- Verify `NOTION_DB_ID` is the database ID (not page ID)

**Need help?** Email IT@ace.nl or ask in this channel!
