from fastapi import APIRouter

from app.jobs.schemas import JobResponse
from app.jobs.service import get_jobs

router = APIRouter()


@router.get("/", response_model=JobResponse)
def get_all_jobs():
    return get_jobs()
