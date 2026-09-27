import os

# Settings require a source database; the tests never connect to one.
os.environ.setdefault("DB_SERVER", "localhost")
os.environ.setdefault("DB_NAME", "test")
