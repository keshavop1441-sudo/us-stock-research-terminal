# Claude Project setup guide

You need: a Claude account on a plan that supports custom Skills and code execution (Pro, Max, Team or Enterprise), and your own SEC contact (name of your app plus an email or URL, e.g. `MyResearch you@example.org`). Menu names change over time; the Claude Help Center articles "Using Skills in Claude" and "Creating and managing projects" are authoritative.

## 1. Build the Skill package
```bash
git clone https://github.com/keshavop1441-sudo/us-stock-research-terminal.git
cd us-stock-research-terminal
python scripts/package_skill.py          # -> dist/us-stock-research.zip (+ .sha256)
python scripts/package_skill.py --verify # optional: rebuild and check it is identical
```
Only the Python standard library is needed to build it.

## 2. Install the Skill
1. In Claude: enable code execution/file creation (Settings -> Capabilities).
2. Open the Skills page (Settings/Customize -> Skills), choose "+", "Upload a skill", and select `dist/us-stock-research.zip`. The ZIP contains the folder `us-stock-research/` with `SKILL.md`.
3. Switch the Skill on. Custom Skills belong to your own account (each person uploads their own).
4. Network access: the engine fetches live data from `www.sec.gov`, `data.sec.gov`, `api.nasdaq.com`, `cdn.cboe.com`. In the code-execution/network settings allow those domains (or full access where your plan/admin permits). Without outbound access the Skill cannot fetch data; it will say so (`doctor --network`). Claude Code on your own computer has normal network access and is the most reliable environment for live ingestion.

## 3. Create the Project
1. Open Projects -> "+ New project"; name it e.g. "US Stock Research".
2. Click "Set project instructions" and paste the whole of `claude/project/project-instructions.md`.
3. Add these four files to Project knowledge (the "+" in the project sidebar), from `claude/skills/us-stock-research/references/`:
   `research-methodology.md`, `metric-definitions.md`, `sources-coverage.md`, `output-templates.md`.
   Do not upload the repository or the other files.

## 4. Use it
Open a chat inside the Project and start with a screening request, for example:

> Screen the stocks AAPL, MSFT, NVDA, AMD, AVGO, CRM, ADBE, INTC for revenue growth above 15%, positive free cash flow and price-to-sales below 15. Show exact values and anything excluded for missing data.

First run: Claude will run the Skill's `doctor`, ask for your SEC contact if none is configured, ingest the symbols (a few minutes), then screen.

## Notes
* Data is stored in a local DuckDB file inside the Skill's working environment. Whether it persists between chats depends on your Claude environment; re-ingesting is idempotent.
* The Skill is not investment advice and gives no recommendations by default.
* Updating: pull the repository, re-run `python scripts/package_skill.py`, re-upload the ZIP (replace the old Skill).
