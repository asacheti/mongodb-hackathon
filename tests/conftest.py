"""Shared test setup. Loads .env so live tests can reach Atlas and the LLM provider."""
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
