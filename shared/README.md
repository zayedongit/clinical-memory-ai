# shared/

Reserved for contracts shared between the frontend and backend — generated API
types, an OpenAPI document, shared enums.

**Currently empty, deliberately.** The frontend declares its own types next to
where they are used. Generating a client from the backend's OpenAPI schema would
remove that duplication and is a reasonable next step; nothing is generated here
yet, and an empty directory is more honest than a stale generated file.

The backend's schema is served at `/openapi.json` when it is running.
