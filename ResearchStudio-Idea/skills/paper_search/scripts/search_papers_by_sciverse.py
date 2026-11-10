"""Optional Sciverse source; credentials and proxy settings come from the environment."""
from sciverse_client import Client, available


def search_papers_by_sciverse(query, start_year, end_year, max_results=10):
    if not available():
        return []
    return Client().search(query, start_year, end_year, max_results)
