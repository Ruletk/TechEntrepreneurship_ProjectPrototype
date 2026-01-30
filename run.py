from dotenv import load_dotenv
from app.config import load_config
from app.db import make_engine, make_session_factory, Base
from app.bot import BotApp


def main():
    load_dotenv()
    cfg = load_config()

    engine = make_engine(cfg.db_url)
    Base.metadata.create_all(engine)

    session_factory = make_session_factory(engine)
    app = BotApp(cfg, session_factory)

    app.bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
