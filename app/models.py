from datetime import datetime

from pydantic import BaseModel, Field


class ProfileRequest(BaseModel):
    url: str = Field(
        min_length=1,
        max_length=500,
        examples=["https://www.linkedin.com/in/vinod-khosla-65387416/"],
    )


class DateValue(BaseModel):
    year: int | None = None
    month: int | None = None
    day: int | None = None


class DateRange(BaseModel):
    start: DateValue | None = None
    end: DateValue | None = None


class Experience(BaseModel):
    title: str | None = None
    company: str | None = None
    company_url: str | None = None
    employment_type: str | None = None
    location: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Education(BaseModel):
    school: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Certification(BaseModel):
    name: str | None = None
    issuer: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None
    date_range: DateRange | None = None


class Language(BaseModel):
    name: str | None = None
    proficiency: str | None = None


class Profile(BaseModel):
    public_identifier: str
    profile_url: str
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    headline: str | None = None
    location: str | None = None
    about: str | None = None
    industry: str | None = None
    follower_count: int | None = None
    connection_count: int | None = None
    profile_image_url: str | None = None
    background_image_url: str | None = None
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)


class ResponseMeta(BaseModel):
    schema_version: str = "1.0"
    fetched_at: datetime
    endpoint: str


class ProfileResponse(BaseModel):
    meta: ResponseMeta
    profile: Profile


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
