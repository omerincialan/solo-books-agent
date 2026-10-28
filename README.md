# SoloBooks Agent

**A back-office assistant for solo tradespeople who live on their phone**: tree services,
mobile mechanics, coaches. They text it like they'd text a bookkeeper, and it keeps their
books in a Google Sheet: expenses, income and business mileage.

Built on **Meta's Muse Spark** (via the Meta Model API) for the
[Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon/details),
Indie track, "Build for Your World" problem statement.

Example conversation (illustrative):

```
Owner:     drove 12 miles to the Henderson job, bought chain oil for 23 bucks, they paid 400 cash
SoloBooks: ✅ Recorded 3 entries:
           🚗 12 mi to Henderson job, deduction $8.40 (T3F9A)
           🧾 Ace Hardware $23.00, Equipment & parts (E81C2)
           💵 $400.00 cash from the Hendersons (I0D47)
Owner:     how much profit this month?
SoloBooks: October so far: $4,850.00 in, $1,212.40 out, profit $3,637.60.
           Plus 214 business miles = $149.80 mileage deduction.
```

## How it works

```
Owner's phone ── SMS / MMS ──> Twilio ──> /sms webhook (signature-checked, owner only)
                                              │  (replies right away; the work runs in the background)
                                              ▼
                                   Muse Spark agent (tool calling)
                       record_expense · record_income · record_trip · update_entry
                       delete_entry · find_entries · get_totals
                                              │
                                              ▼
                     Google Sheet: Expenses / Income / Mileage tabs  ──>  reply texted back
```

- **Muse Spark decides, code does the math.** The model chooses which tools to call; totals,
  profit and mileage deductions are computed in code, so every number it texts back comes
  from a tool result.
- **It double-checks before it records**: possible duplicates and numbers that look like typos
  are confirmed with the owner first (`solobooks/checks.py`).
- **Every entry has an ID** (`E…`, `I…`, `T…`) so "fix that" and "delete the chain oil" hit
  the exact row, and replies can be checked against the sheet.
- **Photos** (receipts) go to Muse Spark as images and are kept on the server, linked from the sheet.
- **Voice notes** are transcribed with Muse Voice Transcribe (needs `ffmpeg` on the server).
- **Shoebox mode:** a photo with several receipts is split with SAM 3.1, one expense per receipt.

| File | What it does |
|---|---|
| `solobooks/agent.py` | Muse Spark tool-calling loop, tools and system prompt |
| `solobooks/books.py` | The Google Sheet ledger (add / update / delete / list, with IDs) |
| `solobooks/server.py` | Twilio webhook, background processing, replies by SMS, photo links |
| `solobooks/voice.py` | Converts voice notes to WAV and transcribes them with Muse Voice Transcribe |
| `solobooks/shoebox.py` | Finds receipts in a photo with SAM 3.1, cuts them out, draws the numbered boxes |
| `solobooks/checks.py` | Duplicate and sanity checks run before anything is recorded |
| `solobooks/trades.py` | Expense categories per trade (tree service, mechanic, tennis coach) |
| `solobooks/memory.py` | Last few turns of the conversation (SQLite) |
| `solobooks/photos.py` | Stores receipt photos behind unguessable links |
| `scripts/smoke_test.py` | Checks your Meta Model API key, tool calling and vision with real calls |
| `tests/` | Tests with a scripted Muse Spark and an in-memory sheet |

## Setup

1. `sudo apt install ffmpeg`, then `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
2. `cp .env.example .env` and fill it in (Meta Model API key, Twilio, owner's number, sheet ID).
3. Google Sheet: create a service account with the Sheets API enabled, save its key as
   `google_service_account.json`, and share the sheet with it as **Editor**.
4. Check the Meta Model API: `venv/bin/python -m scripts.smoke_test`
5. Run: `venv/bin/gunicorn -w 2 solobooks.server:app`, and point the Twilio number's
   **Messaging** webhook to `PUBLIC_URL` (e.g. `http://YOUR-SERVER/sms`, HTTP POST).

Tests: `venv/bin/pip install pytest && venv/bin/python -m pytest`

## Built during the hackathon (Oct 27 – Nov 4, 2026)

Everything in this repository was written during the hackathon; the git history starts on
Oct 27. Progress log:

- **Oct 27** – Agent core: Muse Spark tool-calling agent over a Google Sheet ledger with
  entry IDs; one message can record several entries; exact totals computed in code;
  Twilio webhook with signature check, background replies and conversation memory.
- **Oct 28** – Voice notes: a voice message (AMR, M4A, MP3, OGG… converted with ffmpeg) is
  transcribed by **Muse Voice Transcribe** and handled like a text, so one spoken note can
  create several entries. The reply starts with what was heard, so the owner can check it.
- **Oct 28** – Shoebox mode: one photo of a pile of receipts. **SAM 3.1** finds each receipt
  ("receipt" concept), the photo is cut into one image per receipt, and Muse Spark records
  each as its own expense linked to its own cut-out. The owner gets the photo back with every
  receipt boxed and numbered. Falls back to the whole photo if SAM finds fewer than two.
- **Oct 28** – Checks before recording: likely duplicates (same total and date, or same vendor
  and amount within 3 days) and implausible values (amount far above normal, tax ≥ total or
  over 20%, dates in the future or over a year old, 400+ mile trips) are not recorded; the
  agent asks the owner and records only once they confirm. `get_totals` also lists entries
  already recorded twice, so "how much profit this month?" comes with "E3F9A and E81C2 look
  like the same receipt".
- *Next:* deploy and test against the live Meta models, tree-service demo, video.
