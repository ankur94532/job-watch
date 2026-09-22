from .amazon import AmazonAdapter
from .arm import ArmAdapter
from .ashby import AshbyAdapter
from .bain import BainAdapter
from .bamboohr import BambooHRAdapter
from .atlassian import AtlassianAdapter
from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting
from .embedded import AppleAdapter, GoogleAdapter
from .eightfold import EightfoldAdapter
from .comeet import ComeetAdapter
from .gem import GemAdapter
from .goldman import GoldmanAdapter
from .greenhouse import GreenhouseAdapter
from .ibm import IBMAdapter
from .html import GlobalLogicAdapter, KulaAdapter, MediaNetAdapter, SkimaAdapter
from .jibe import JibeAdapter
from .jobvite import JobviteAdapter
from .schema_sitemap import SchemaSitemapAdapter
from .siemens import SiemensAdapter
from .public_html import TrakstarAdapter, SuccessFactorsHTMLAdapter
from .lever import LeverAdapter
from .maersk import MaerskAdapter
from .mathworks import MathWorksAdapter
from .meta import MetaAdapter
from .mckinsey import McKinseyAdapter
from .microsoft import MicrosoftAdapter
from .citadel import CitadelSecuritiesAdapter
from .cisco import CiscoAdapter
from .instahyre import InstahyreAdapter
from .navi import NaviAdapter
from .oracle import OracleAdapter
from .ownfeed import DEShawAdapter, QuadeyeAdapter
from .phenom import PhenomAdapter
from .radancy import RadancyAdapter, SitemapAdapter
from .rippling import RipplingAdapter
from .smartrecruiters import SmartRecruitersAdapter
from .smartrecruiters_html import SmartRecruitersHTMLAdapter
from .spire2grow import Spire2GrowAdapter
from .workday import WorkdayAdapter
from .zwayam import ZwayamAdapter

ADAPTERS = {
    a.ats: a
    for a in (
        # multi-tenant ATS platforms
        GreenhouseAdapter, LeverAdapter, AshbyAdapter, SmartRecruitersAdapter,
        WorkdayAdapter, EightfoldAdapter, RipplingAdapter, RadancyAdapter, OracleAdapter,
        JibeAdapter, Spire2GrowAdapter, PhenomAdapter, BambooHRAdapter, ZwayamAdapter,
        # single-company public JSON boards
        AmazonAdapter, AtlassianAdapter, GoldmanAdapter, NaviAdapter, BainAdapter, MetaAdapter,
        InstahyreAdapter, MaerskAdapter, CiscoAdapter, IBMAdapter, MathWorksAdapter,
        McKinseyAdapter, CitadelSecuritiesAdapter,
        # server-rendered pages whose search state is embedded JSON
        AppleAdapter, GoogleAdapter, QuadeyeAdapter, DEShawAdapter, GemAdapter, ComeetAdapter,
        # careers pages with no job API: parsed from the HTML (see adapters/html.py)
        MediaNetAdapter, SkimaAdapter, KulaAdapter, GlobalLogicAdapter,
        # careers sites read from the job sitemap they publish for crawlers
        SitemapAdapter, SchemaSitemapAdapter, JobviteAdapter, SiemensAdapter,
        TrakstarAdapter, SuccessFactorsHTMLAdapter, SmartRecruitersHTMLAdapter, ArmAdapter,
    )
}
# Specialized selector. Its latest-page poll is independent of Eightfold's
# shared tenant slot; the adapter paces its own bounded closure API requests.
ADAPTERS["microsoft"] = MicrosoftAdapter

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
