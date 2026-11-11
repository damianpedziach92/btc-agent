
from sqlalchemy import text

from database.connection import engine


def test_connection():
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT current_database(), current_user")
        )

        database_name, username = result.one()

        print(f"Baza danych: {database_name}")
        print(f"Użytkownik: {username}")
        print("Połączenie działa!")


if __name__ == "__main__":
    test_connection()
