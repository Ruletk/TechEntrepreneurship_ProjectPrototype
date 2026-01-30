# Starting up

To start project, copy `.env.default` to `.env`, insert telegram bot token.

Then type

## Windows
```powershell
python -m venv .venv
./.venv/Scripts/activate
pip install -r requirements.txt
python run.py
```

## Mac/Linux
```bash
python3 -m venv .venv
./.venv/bin/activate
python3 -m pip install -r requirements.txt
python3 run.py
```