from fastapi import FastAPI

from app.jobs.router import router as jobs_router

app = FastAPI(title="Job Board Backend")

app.include_router(jobs_router, prefix="/jobs", tags=["Jobs"])


@app.get("/")
def root():
    return {"message": "Job Board Backend is running!"}