from .amazon import AmazonAdapter
from .ashby import AshbyAdapter
from .atlassian import AtlassianAdapter
from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting
from .embedded import AppleAdapter, GoogleAdapter
from .eightfold import EightfoldAdapter
from .greenhouse import GreenhouseAdapter
from .jibe import JibeAdapter
from .lever import LeverAdapter
from .oracle import OracleAdapter
from .radancy import RadancyAdapter
from .rippling import RipplingAdapter
from .smartrecruiters import SmartRecruitersAdapter
from .spire2grow import Spire2GrowAdapter
from .workday import WorkdayAdapter

ADAPTERS = {
    a.ats: a
    for a in (
        # multi-tenant ATS platforms
        GreenhouseAdapter, LeverAdapter, AshbyAdapter, SmartRecruitersAdapter,
        WorkdayAdapter, EightfoldAdapter, RipplingAdapter, RadancyAdapter, OracleAdapter,
        JibeAdapter, Spire2GrowAdapter,
        # single-company public JSON boards
        AmazonAdapter, AtlassianAdapter,
        # server-rendered pages whose search state is embedded JSON
        AppleAdapter, GoogleAdapter,
    )
}

UNSUPPORTED = "unsupported"


def build_adapter(company, cfg, http=None, page_delay=0.5):
    ats = cfg.get("ats")
    if ats not in ADAPTERS:
        raise AdapterError(f"{company}: no adapter for ats={ats!r}")
    return ADAPTERS[ats](company, cfg, http=http, page_delay=page_delay)


__all__ = [
    "ADAPTERS", "UNSUPPORTED", "Adapter", "AdapterError", "FetchContext", "FetchResult",
    "Posting", "build_adapter",
]
