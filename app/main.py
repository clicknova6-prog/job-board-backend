from fastapi import FastAPI

app = FastAPI(
    title="Job Board API",
    version="1.0.0",
)


@app.get("/")
def root():
    return {
        "status": "success",
        "message": "Job Board API is running."
    }
