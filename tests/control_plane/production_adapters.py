"""Production-backed replay adapter assembled from thin test-only mixins."""
from tests.control_plane.production_adapter_base import AdapterBase
from tests.control_plane.production_cases_core import CoreCasesMixin
from tests.control_plane.production_cases_p0a import P0CasesAMixin
from tests.control_plane.production_cases_p0b import P0CasesBMixin


class ProductionReplayAdapter(P0CasesBMixin, P0CasesAMixin, CoreCasesMixin, AdapterBase):
    pass


from tests.control_plane.production_adapter_base import IntegrationBindingUnavailable
