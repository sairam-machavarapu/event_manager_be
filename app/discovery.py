"""Public, bounded card listings. No membership or private event data is exposed."""

import base64
import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app import discovery_cache
from app.auth import DB
from app.models import (
    EVENT_SEARCH_DOCUMENT,
    DiscoveryVersion,
    Event,
    EventMedia,
    Organizer,
    TicketType,
)
from app.storage import public_url

router = APIRouter(prefix="/api/v1/events", tags=["public discovery"])
SEARCH_DOCUMENT = EVENT_SEARCH_DOCUMENT


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=120)
    category: (
        Literal["Music", "Workshops", "Community", "Talks", "Food & drink", "Outdoors", "Other"]
        | None
    ) = None
    date_from: date | None = None
    date_to: date | None = None
    price: Literal["all", "free", "paid"] = "all"
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def normalize(self):
        self.q = " ".join(re.findall(r"\w+", self.q.casefold()))
        self.city = self.city.strip().casefold()
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValueError("date_to must be on or after date_from")
        # The exclusive upper bound needs a representable next day.
        if self.date_to == date.max:
            raise ValueError("date_to is outside the supported range")
        return self


class Host(BaseModel):
    name: str
    slug: str


class EventCard(BaseModel):
    id: UUID
    slug: str
    title: str
    category: str
    format: str
    timezone: str
    starts_at: datetime
    ends_at: datetime
    venue: str | None
    city: str | None
    cover_url: str | None
    organizer: Host
    price_minor: int
    currency: Literal["INR"] = "INR"


class EventPage(BaseModel):
    items: list[EventCard]
    next_cursor: str | None


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fingerprint(filters):
    data = filters.model_dump(mode="json", exclude={"cursor", "limit"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def encode_cursor(card, filters):
    data = [1, utc(card.starts_at).isoformat(), str(card.id), fingerprint(filters)]
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def decode_cursor(value, filters):
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        version, timestamp, event_id, digest = json.loads(raw)
        start = datetime.fromisoformat(timestamp)
        if version != 1 or digest != fingerprint(filters) or start.utcoffset() is None:
            raise ValueError
        return utc(start), UUID(event_id)
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise HTTPException(
            422, "Invalid cursor or changed filters; restart from the first page"
        ) from exc


def listing_query(filters, now, dialect):
    # Correlated scalar lookups use event indexes, rather than loading ORM graphs
    # or aggregating every ticket/media row for each small page.
    price = (
        sa.select(sa.func.min(TicketType.price_minor))
        .where(TicketType.event_id == Event.id, TicketType.capacity > 0)
        .correlate(Event)
        .scalar_subquery()
    )
    poster = (
        sa.select(EventMedia.image_key)
        .where(
            EventMedia.event_id == Event.id,
            EventMedia.kind == "poster",
            EventMedia.status == "ready",
        )
        .order_by(EventMedia.created_at.desc(), EventMedia.id)
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    query = (
        sa.select(
            Event.id,
            Event.slug,
            Event.title,
            Event.category,
            Event.format,
            Event.timezone,
            Event.starts_at,
            Event.ends_at,
            Event.venue,
            Event.city,
            Event.cover_url,
            Organizer.name.label("host_name"),
            Organizer.slug.label("host_slug"),
            price.label("price_minor"),
            poster.label("poster_key"),
        )
        .join(Organizer, Organizer.id == Event.organizer_id)
        .where(
            Event.status == "published",
            Organizer.status == "approved",
            Event.starts_at >= now,
            Event.ends_at.is_not(None),
            Event.title.is_not(None),
            Event.category.is_not(None),
            price.is_not(None),
        )
    )
    if filters.q:
        if dialect == "postgresql":
            query = query.where(
                sa.literal_column(SEARCH_DOCUMENT).op("@@")(
                    sa.func.plainto_tsquery(sa.literal_column("'simple'"), filters.q)
                )
            )
        else:
            # SQLite is only the test adapter; production uses indexed lexeme search.
            for word in filters.q.split():
                query = query.where(
                    sa.or_(
                        *(
                            column.icontains(word, autoescape=True)
                            for column in (Event.title, Event.description, Event.venue, Event.city)
                        )
                    )
                )
    if filters.city:
        query = query.where(sa.func.lower(Event.city) == filters.city)
    if filters.category:
        query = query.where(Event.category == filters.category)
    if filters.date_from:
        query = query.where(Event.starts_at >= datetime.combine(filters.date_from, time.min, UTC))
    if filters.date_to:
        query = query.where(
            Event.starts_at < datetime.combine(filters.date_to, time.min, UTC) + timedelta(days=1)
        )
    if filters.price == "free":
        query = query.where(price == 0)
    elif filters.price == "paid":
        query = query.where(price > 0)
    if filters.cursor:
        start, event_id = decode_cursor(filters.cursor, filters)
        query = query.where(
            sa.or_(Event.starts_at > start, sa.and_(Event.starts_at == start, Event.id > event_id))
        )
    return query.order_by(Event.starts_at, Event.id).limit(filters.limit + 1)


@router.get("", response_model=EventPage)
async def discover_events(filters: Annotated[Filters, Query()], db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if filters.cursor:
        decode_cursor(filters.cursor, filters)
    now = datetime.now(UTC)
    version = await db.scalar(sa.select(DiscoveryVersion.revision).where(DiscoveryVersion.id == 1))
    cache_key = discovery_cache.key("cards", version, filters.model_dump_json())
    cached = await discovery_cache.read(cache_key)
    if cached:
        try:
            page = EventPage.model_validate_json(cached)
            if all(
                item.starts_at.utcoffset() is not None and item.starts_at >= now
                for item in page.items
            ):
                return page
        except ValidationError:
            pass
    rows = (
        (await db.execute(listing_query(filters, now, db.get_bind().dialect.name))).mappings().all()
    )
    items = []
    for row in rows[: filters.limit]:
        data = dict(row)
        poster_key = data.pop("poster_key")
        data["cover_url"] = data["cover_url"] or public_url(poster_key)
        data["organizer"] = Host(name=data.pop("host_name"), slug=data.pop("host_slug"))
        data["starts_at"], data["ends_at"] = utc(data["starts_at"]), utc(data["ends_at"])
        items.append(EventCard(**data))
    page = EventPage(
        items=items,
        next_cursor=encode_cursor(items[-1], filters) if len(rows) > filters.limit else None,
    )
    await discovery_cache.write(cache_key, page.model_dump_json())
    return page


@router.get("/cities", response_model=list[str])
async def discover_cities(db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    version = await db.scalar(sa.select(DiscoveryVersion.revision).where(DiscoveryVersion.id == 1))
    cache_key = discovery_cache.key("cities", version)
    cached = await discovery_cache.read(cache_key)
    if cached:
        try:
            cities = json.loads(cached)
            if isinstance(cities, list) and all(isinstance(city, str) for city in cities):
                return cities
        except ValueError, TypeError:
            pass
    query = (
        sa.select(sa.func.min(Event.city))
        .join(Organizer, Organizer.id == Event.organizer_id)
        .where(
            Event.status == "published",
            Organizer.status == "approved",
            Event.starts_at >= datetime.now(UTC),
            Event.city.is_not(None),
            Event.city != "",
            sa.exists(
                sa.select(TicketType.id).where(
                    TicketType.event_id == Event.id,
                    TicketType.capacity > 0,
                )
            ),
        )
        .group_by(sa.func.lower(Event.city))
        .order_by(sa.func.lower(Event.city))
        .limit(200)
    )
    cities = list((await db.execute(query)).scalars())
    await discovery_cache.write(cache_key, json.dumps(cities))
    return cities


class PublicTicket(BaseModel):
    id: UUID
    name: str
    price_minor: int
    currency: str
    per_order_limit: int
    sales_start: datetime | None
    sales_end: datetime | None
    sales_status: Literal["upcoming", "open", "closed", "unavailable"]


class PublicImage(BaseModel):
    url: str
    alt_text: str


class EventDetail(EventCard):
    description: str
    status: Literal["published", "cancelled"]
    cancellation_reason: str | None
    ended: bool
    tickets: list[PublicTicket]
    gallery: list[PublicImage]


@router.get("/{slug}", response_model=EventDetail)
async def event_detail(slug: str, db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    row = (
        await db.execute(
            sa.select(Event, Organizer)
            .join(Organizer, Organizer.id == Event.organizer_id)
            .where(
                Event.slug == slug,
                Event.status.in_(["published", "cancelled"]),
                Organizer.status == "approved",
                Event.starts_at.is_not(None),
                Event.ends_at.is_not(None),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Event not found")
    event, host = row
    now = datetime.now(UTC)
    ended = utc(event.ends_at) <= now
    tickets = list(
        (
            await db.execute(
                sa.select(TicketType)
                .where(TicketType.event_id == event.id)
                .order_by(TicketType.price_minor, TicketType.id)
            )
        )
        .scalars()
        .all()
    )
    media = list(
        (
            await db.execute(
                sa.select(EventMedia)
                .where(
                    EventMedia.event_id == event.id,
                    EventMedia.status == "ready",
                    EventMedia.image_key.is_not(None),
                )
                .order_by(EventMedia.created_at.desc(), EventMedia.id)
            )
        )
        .scalars()
        .all()
    )

    def sales_status(ticket):
        if event.status == "cancelled" or ended or ticket.capacity == 0:
            return "unavailable"
        if ticket.sales_end and utc(ticket.sales_end) <= now:
            return "closed"
        if ticket.sales_start and utc(ticket.sales_start) > now:
            return "upcoming"
        return "open"

    prices = [ticket.price_minor for ticket in tickets if ticket.capacity > 0]
    return EventDetail(
        id=event.id,
        slug=event.slug,
        title=event.title,
        category=event.category,
        format=event.format,
        timezone=event.timezone,
        starts_at=utc(event.starts_at),
        ends_at=utc(event.ends_at),
        venue=event.venue,
        city=event.city,
        cover_url=event.cover_url
        or next((public_url(image.image_key) for image in media if image.kind == "poster"), None),
        organizer=Host(name=host.name, slug=host.slug),
        price_minor=min(prices, default=0),
        description=event.description or "",
        status=event.status,
        cancellation_reason=event.cancellation_reason,
        ended=ended,
        tickets=[
            PublicTicket(
                id=t.id,
                name=t.name,
                price_minor=t.price_minor,
                currency=t.currency,
                per_order_limit=t.per_order_limit,
                sales_start=utc(t.sales_start) if t.sales_start else None,
                sales_end=utc(t.sales_end) if t.sales_end else None,
                sales_status=sales_status(t),
            )
            for t in tickets
        ],
        gallery=[
            PublicImage(url=public_url(image.image_key), alt_text=image.alt_text)
            for image in media
            if image.kind == "gallery"
        ],
    )
