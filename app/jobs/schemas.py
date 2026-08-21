from pydantic import BaseModel


class JobResponse(BaseModel):
    message: str
