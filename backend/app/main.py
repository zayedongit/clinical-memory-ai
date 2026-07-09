"""FastAPI application entrypoint."""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routers import clinics, health, match, me, patients

app = FastAPI(title="Clinical Memory AI API", version="0.1.0")

# CORS: allow the local/deployed frontend to call this API.
# Read directly from env so the app imports without a full settings load.
_frontend_origin = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(me.router)
app.include_router(clinics.router)
app.include_router(patients.router)
app.include_router(match.router)
