"""``python -m callscore_api.openapi`` prints the OpenAPI schema as JSON.

Used to generate the web client's TypeScript types.
"""

import json

from callscore_api.app import create_app

if __name__ == "__main__":
    print(json.dumps(create_app().openapi()))
