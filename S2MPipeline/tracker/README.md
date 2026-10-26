# Tracker — Deliverable / Ticket / Bug flow

A minimal but faithful implementation of the DLV-1 flow in the schema:
Project → Deliverable → Tickets → Bugs (auto `Blocks`) → three-step
bug lifecycle → readiness-gated acceptance.

## Run

```bash
python -m venv .venv
source ../.venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py