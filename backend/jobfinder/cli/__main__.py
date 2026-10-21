"""`python -m jobfinder.cli ...`: the same Typer app as the `jobfinder` script (the pre-#49 names are gone, #53)."""
from jobfinder.cli import app

if __name__ == "__main__":
    app(prog_name="jobfinder")
