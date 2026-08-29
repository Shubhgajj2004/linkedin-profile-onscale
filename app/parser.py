from typing import Any

from .models import (
    Certification,
    DateRange,
    DateValue,
    Education,
    Experience,
    Language,
    Profile,
)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "name", "localizedName", "defaultLocalizedName"):
            result = _text(value.get(key))
            if result:
                return result
    return None


def _date(value: Any) -> DateValue | None:
    if not isinstance(value, dict):
        return None
    date = DateValue(
        year=value.get("year"), month=value.get("month"), day=value.get("day")
    )
    return date if date.year or date.month or date.day else None


def _date_range(value: Any) -> DateRange | None:
    if not isinstance(value, dict):
        return None
    result = DateRange(
        start=_date(value.get("start") or value.get("startDate")),
        end=_date(value.get("end") or value.get("endDate")),
    )
    return result if result.start or result.end else None


def _image_url(value: Any, depth: int = 0) -> str | None:
    if not isinstance(value, dict) or depth > 8:
        return None
    root = value.get("rootUrl")
    artifacts = value.get("artifacts")
    if isinstance(root, str) and isinstance(artifacts, list):
        candidates = [item for item in artifacts if isinstance(item, dict)]
        largest = max(
            candidates,
            key=lambda item: int(item.get("width", 0)) * int(item.get("height", 0)),
            default=None,
        )
        if largest:
            segment = largest.get("fileIdentifyingUrlPathSegment") or largest.get("url")
            if isinstance(segment, str):
                return segment if segment.startswith("http") else root + segment
    for key in (
        "vectorImage",
        "com.linkedin.common.VectorImage",
        "displayImageReference",
        "displayImage",
        "picture",
    ):
        result = _image_url(value.get(key), depth + 1)
        if result:
            return result
    return None


def _resolve(
    entity: dict[str, Any], urns: dict[str, dict[str, Any]], *keys: str
) -> dict[str, Any]:
    for key in keys:
        value = entity.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value in urns:
            return urns[value]
    return {}


def _kind(entity: dict[str, Any]) -> str:
    type_name = str(entity.get("$type", "")).lower()
    urn = str(entity.get("entityUrn", "")).lower()
    for type_suffix, urn_tag, result in (
        (".position", ":fsd_profileposition:", "experience"),
        (".education", ":fsd_profileeducation:", "education"),
        (".skill", ":fsd_profileskill:", "skill"),
        (".certification", ":fsd_profilecertification:", "certification"),
        (".language", ":fsd_profilelanguage:", "language"),
    ):
        if type_name.endswith(type_suffix) or urn_tag in urn:
            return result
    return ""


def _dedupe(items: list[Any], key) -> list[Any]:
    result, seen = [], set()
    for item in items:
        marker = key(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def parse_profile(
    payload: dict[str, Any], identifier: str, canonical_url: str
) -> Profile:
    included = [item for item in payload.get("included", []) if isinstance(item, dict)]
    urns = {
        item["entityUrn"]: item
        for item in included
        if isinstance(item.get("entityUrn"), str)
    }
    modern = next(
        (
            item
            for item in included
            if item.get("publicIdentifier") == identifier
            and "profile" in str(item.get("$type", "")).lower()
        ),
        None,
    )
    legacy = (
        payload.get("profile") if isinstance(payload.get("profile"), dict) else None
    )
    base = modern or legacy
    if not base:
        raise ValueError("LinkedIn returned no recognizable profile")

    mini = base.get("miniProfile") if isinstance(base.get("miniProfile"), dict) else {}
    first_name = _text(base.get("firstName")) or _text(mini.get("firstName"))
    last_name = _text(base.get("lastName")) or _text(mini.get("lastName"))
    geo = _resolve(base, urns, "*geo", "geo")
    if not geo and isinstance(base.get("geoLocation"), dict):
        geo = _resolve(base["geoLocation"], urns, "*geo", "geoUrn")
    industry = _resolve(base, urns, "*industry", "industry")

    profile = Profile(
        public_identifier=identifier,
        profile_url=canonical_url,
        first_name=first_name,
        last_name=last_name,
        name=" ".join(part for part in (first_name, last_name) if part) or None,
        headline=_text(base.get("headline")) or _text(mini.get("occupation")),
        location=(
            _text(base.get("geoLocationName"))
            or _text(base.get("locationName"))
            or _text(geo)
        ),
        about=_text(base.get("summary")),
        industry=_text(base.get("industryName")) or _text(industry),
        follower_count=base.get("followerCount"),
        connection_count=base.get("connectionCount"),
        profile_image_url=_image_url(base.get("profilePicture"))
        or _image_url(base)
        or _image_url(mini),
        background_image_url=_image_url(base.get("backgroundPicture"))
        or _image_url(base.get("backgroundImage")),
    )

    entities = included[:]
    legacy_views = {
        "positionView": "experience",
        "educationView": "education",
        "skillView": "skill",
        "certificationView": "certification",
        "languageView": "language",
    }
    for view_name, kind in legacy_views.items():
        view = payload.get(view_name)
        if not isinstance(view, dict):
            continue
        for item in view.get("elements", []):
            if isinstance(item, dict):
                entities.append({**item, "__kind": kind})

    for entity in entities:
        kind = entity.get("__kind") or _kind(entity)
        if kind == "experience":
            company = _resolve(entity, urns, "*company", "company")
            company_slug = company.get("universalName")
            profile.experience.append(
                Experience(
                    title=_text(entity.get("title")),
                    company=_text(entity.get("companyName")) or _text(company),
                    company_url=(
                        f"https://www.linkedin.com/company/{company_slug}/"
                        if company_slug
                        else None
                    ),
                    employment_type=_text(entity.get("employmentType")),
                    location=_text(entity.get("locationName"))
                    or _text(entity.get("geoLocationName")),
                    description=_text(entity.get("description")),
                    date_range=_date_range(
                        entity.get("dateRange") or entity.get("timePeriod")
                    ),
                )
            )
        elif kind == "education":
            school = _resolve(entity, urns, "*school", "school")
            profile.education.append(
                Education(
                    school=_text(entity.get("schoolName")) or _text(school),
                    degree=_text(entity.get("degreeName")),
                    field_of_study=_text(entity.get("fieldOfStudy")),
                    description=_text(entity.get("description")),
                    date_range=_date_range(
                        entity.get("dateRange") or entity.get("timePeriod")
                    ),
                )
            )
        elif kind == "skill":
            name = _text(entity.get("name"))
            if name:
                profile.skills.append(name)
        elif kind == "certification":
            profile.certifications.append(
                Certification(
                    name=_text(entity.get("name")),
                    issuer=_text(entity.get("authority"))
                    or _text(entity.get("issuer")),
                    credential_id=_text(entity.get("licenseNumber"))
                    or _text(entity.get("credentialId")),
                    credential_url=_text(entity.get("url")),
                    date_range=_date_range(
                        entity.get("dateRange") or entity.get("timePeriod")
                    ),
                )
            )
        elif kind == "language":
            profile.languages.append(
                Language(
                    name=_text(entity.get("name")),
                    proficiency=_text(entity.get("proficiency")),
                )
            )

    profile.experience = _dedupe(
        profile.experience,
        lambda item: (item.title, item.company, repr(item.date_range)),
    )
    profile.education = _dedupe(
        profile.education,
        lambda item: (item.school, item.degree, item.field_of_study),
    )
    profile.skills = list(dict.fromkeys(profile.skills))
    profile.certifications = _dedupe(
        profile.certifications,
        lambda item: (item.name, item.issuer, item.credential_id),
    )
    profile.languages = _dedupe(
        profile.languages,
        lambda item: (item.name, item.proficiency),
    )
    return profile
