from enum import StrEnum


class RunMode(StrEnum):
    """Data-source operating mode. Independent of the deployment environment."""

    REPLAY = "REPLAY"
    HYBRID = "HYBRID"
    LIVE = "LIVE"


class SourceFamily(StrEnum):
    """Coarse source class. Not an independence group."""

    SEC_REGULATORY = "SEC_REGULATORY"
    ISSUER_OFFICIAL = "ISSUER_OFFICIAL"
    NEWS_WIRE = "NEWS_WIRE"
    NEWS_OUTLET = "NEWS_OUTLET"
    REPLAY_SOCIAL = "REPLAY_SOCIAL"
    UNKNOWN = "UNKNOWN"


class LifecycleStatus(StrEnum):
    EMERGING = "EMERGING"
    CORROBORATED = "CORROBORATED"
    CONTESTED = "CONTESTED"
    CONFIRMED = "CONFIRMED"
    AMENDED = "AMENDED"
    RESOLVED = "RESOLVED"
    DENIED = "DENIED"


class EvidenceStrength(StrEnum):
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"
    CONTESTED = "CONTESTED"


class PolicyRoute(StrEnum):
    SCENARIO = "SCENARIO"
    REVIEW = "REVIEW"
    MONITOR = "MONITOR"


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    BOND = "BOND"
    LOAN = "LOAN"


class MaterialityBand(StrEnum):
    """Event materiality. Deliberately distinct from `ScenarioBand`."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScenarioBand(StrEnum):
    """Scenario severity. Deliberately distinct from `MaterialityBand`."""

    LOW = "low"
    CENTRAL = "central"
    ADVERSE = "adverse"
