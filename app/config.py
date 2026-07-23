from dotenv import load_dotenv
import os

load_dotenv()

class Settings:
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    BASE_DIR: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    GLOSSARY_DIR: str = os.getenv("GLOSSARY_DIR", os.path.join(BASE_DIR, "glossaries"))
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
    JOBS_DIR: str = os.getenv("JOBS_DIR", os.path.join(BASE_DIR, "jobs"))

settings = Settings()
