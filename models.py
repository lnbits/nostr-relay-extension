import json
from sqlite3 import Row
from typing import Any, List, Optional, Tuple

from pydantic import BaseModel, Field

from .relay.event import NostrEvent


class Spec(BaseModel):
    class Config:
        allow_population_by_field_name = True


class FilterSpec(Spec):
    max_client_filters = Field(0, alias="maxClientFilters")
    limit_per_filter = Field(1000, alias="limitPerFilter")


class EventSpec(Spec):
    max_events_per_hour = Field(0, alias="maxEventsPerHour")

    created_at_days_past = Field(0, alias="createdAtDaysPast")
    created_at_hours_past = Field(0, alias="createdAtHoursPast")
    created_at_minutes_past = Field(0, alias="createdAtMinutesPast")
    created_at_seconds_past = Field(0, alias="createdAtSecondsPast")

    created_at_days_future = Field(0, alias="createdAtDaysFuture")
    created_at_hours_future = Field(0, alias="createdAtHoursFuture")
    created_at_minutes_future = Field(0, alias="createdAtMinutesFuture")
    created_at_seconds_future = Field(0, alias="createdAtSecondsFuture")

    @property
    def created_at_in_past(self) -> int:
        return (
            self.created_at_days_past * 86400
            + self.created_at_hours_past * 3600
            + self.created_at_minutes_past * 60
            + self.created_at_seconds_past
        )

    @property
    def created_at_in_future(self) -> int:
        return (
            self.created_at_days_future * 86400
            + self.created_at_hours_future * 3600
            + self.created_at_minutes_future * 60
            + self.created_at_seconds_future
        )


class StorageSpec(Spec):
    free_storage_value = Field(1, alias="freeStorageValue")
    free_storage_unit = Field("MB", alias="freeStorageUnit")
    full_storage_action = Field("prune", alias="fullStorageAction")

    @property
    def free_storage_bytes_value(self):
        value = self.free_storage_value * 1024
        if self.free_storage_unit == "MB":
            value *= 1024
        return value


class AuthSpec(BaseModel):
    require_auth_events = Field(False, alias="requireAuthEvents")
    skiped_auth_events = Field([], alias="skipedAuthEvents")
    forced_auth_events = Field([], alias="forcedAuthEvents")
    require_auth_filter = Field(False, alias="requireAuthFilter")

    def event_requires_auth(self, kind: int) -> bool:
        if self.require_auth_events:
            return kind not in self.skiped_auth_events
        return kind in self.forced_auth_events


class PaymentSpec(BaseModel):
    is_paid_relay = Field(False, alias="isPaidRelay")
    cost_to_join = Field(0, alias="costToJoin")

    storage_cost_value = Field(0, alias="storageCostValue")
    storage_cost_unit = Field("MB", alias="storageCostUnit")


class WalletSpec(Spec):
    wallet = Field("")


class RelayPublicSpec(FilterSpec, EventSpec, StorageSpec, PaymentSpec):
    domain: str = ""

    @property
    def is_read_only_relay(self):
        self.free_storage_value == 0 and not self.is_paid_relay


class RelaySpec(RelayPublicSpec, WalletSpec, AuthSpec):
    pass


class NostrRelay(BaseModel):
    id: str
    name: str
    description: Optional[str]
    pubkey: Optional[str]
    contact: Optional[str]
    active: bool = False

    config: "RelaySpec" = RelaySpec()

    @property
    def is_free_to_join(self):
        return not self.config.is_paid_relay or self.config.cost_to_join == 0

    @classmethod
    def from_row(cls, row: Row) -> "NostrRelay":
        relay = cls(**dict(row))
        relay.config = RelaySpec(**json.loads(row["meta"]))
        return relay

    @classmethod
    def info(
        cls,
    ) -> dict:
        return {
            "contact": "https://t.me/lnbits",
            "supported_nips": [1, 9, 11, 15, 20, 22, 42],
            "software": "LNbits",
            "version": "",
        }

class NostrFilter(BaseModel):
    subscription_id: Optional[str]

    ids: List[str] = []
    authors: List[str] = []
    kinds: List[int] = []
    e: List[str] = Field([], alias="#e")
    p: List[str] = Field([], alias="#p")
    since: Optional[int]
    until: Optional[int]
    limit: Optional[int]

    def matches(self, e: NostrEvent) -> bool:
        # todo: starts with
        if len(self.ids) != 0 and e.id not in self.ids:
            return False
        if len(self.authors) != 0 and e.pubkey not in self.authors:
            return False
        if len(self.kinds) != 0 and e.kind not in self.kinds:
            return False

        if self.since and e.created_at < self.since:
            return False
        if self.until and self.until > 0 and e.created_at > self.until:
            return False

        found_e_tag = self.tag_in_list(e.tags, "e")
        found_p_tag = self.tag_in_list(e.tags, "p")
        if not found_e_tag or not found_p_tag:
            return False

        return True

    def tag_in_list(self, event_tags, tag_name) -> bool:
        filter_tags = dict(self).get(tag_name, [])
        if len(filter_tags) == 0:
            return True

        event_tag_values = [t[1] for t in event_tags if t[0] == tag_name]

        common_tags = [
            event_tag for event_tag in event_tag_values if event_tag in filter_tags
        ]
        if len(common_tags) == 0:
            return False
        return True

    def is_empty(self):
        return (
            len(self.ids) == 0
            and len(self.authors) == 0
            and len(self.kinds) == 0
            and len(self.e) == 0
            and len(self.p) == 0
            and (not self.since)
            and (not self.until)
        )

    def enforce_limit(self, limit: int):
        if not self.limit or self.limit > limit:
            self.limit = limit

    def to_sql_components(
        self, relay_id: str
    ) -> Tuple[List[str], List[str], List[Any]]:
        inner_joins: List[str] = []
        where = ["deleted=false", "nostrrelay.events.relay_id = ?"]
        values: List[Any] = [relay_id]

        if len(self.e):
            values += self.e
            e_s = ",".join(["?"] * len(self.e))
            inner_joins.append(
                "INNER JOIN nostrrelay.event_tags e_tags ON nostrrelay.events.id = e_tags.event_id"
            )
            where.append(f" (e_tags.value in ({e_s}) AND e_tags.name = 'e')")

        if len(self.p):
            values += self.p
            p_s = ",".join(["?"] * len(self.p))
            inner_joins.append(
                "INNER JOIN nostrrelay.event_tags p_tags ON nostrrelay.events.id = p_tags.event_id"
            )
            where.append(f" p_tags.value in ({p_s}) AND p_tags.name = 'p'")

        if len(self.ids) != 0:
            ids = ",".join(["?"] * len(self.ids))
            where.append(f"id IN ({ids})")
            values += self.ids

        if len(self.authors) != 0:
            authors = ",".join(["?"] * len(self.authors))
            where.append(f"pubkey IN ({authors})")
            values += self.authors

        if len(self.kinds) != 0:
            kinds = ",".join(["?"] * len(self.kinds))
            where.append(f"kind IN ({kinds})")
            values += self.kinds

        if self.since:
            where.append("created_at >= ?")
            values += [self.since]

        if self.until:
            where.append("created_at < ?")
            values += [self.until]

        return inner_joins, where, values


class BuyOrder(BaseModel):
    action: str
    relay_id: str
    pubkey: str
    units_to_buy = 0

    def is_valid_action(self):
        return self.action in ["join", "storage"]


class NostrPartialAccount(BaseModel):
    relay_id: str
    pubkey: str
    allowed: Optional[bool]
    blocked: Optional[bool]


class NostrAccount(BaseModel):
    pubkey: str
    allowed = False
    blocked = False
    sats = 0
    storage = 0
    paid_to_join = False

    @property
    def can_join(self):
        """If an account is explicitly allowed then it does not need to pay"""
        return self.paid_to_join or self.allowed

    @classmethod
    def null_account(cls) -> "NostrAccount":
        return NostrAccount(pubkey="")

    @classmethod
    def from_row(cls, row: Row) -> "NostrAccount":
        return cls(**dict(row))
