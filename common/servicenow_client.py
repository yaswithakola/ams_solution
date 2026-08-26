"""
Thin REST client around the ServiceNow Table API.

Credentials are read from config.ServiceNowSettings, which in turn reads
SERVICENOW_USERNAME / SERVICENOW_PASSWORD from the environment.
PLACEHOLDER VALUES are used if nothing is configured - replace them with
real credentials (ideally injected via a secrets manager, not hard-coded).

--------------------------------------------------------------------------
TROUBLESHOOTING "failing to connect" / 401 against a ServiceNow instance
--------------------------------------------------------------------------
1. SERVICENOW_INSTANCE_URL must be the bare instance root only, e.g.
       https://dev198124.service-now.com
   Not a login.do URL with a query string - that only exercises the
   browser UI session login, not REST Basic Auth. This client strips a
   path/query string if you leave one in by mistake, but fix the env var.

2. Passwords copied out of a login.do test URL are URL-ENCODED. If your
   password contains "%2F" or "%5E" etc, decode it first (%2F -> "/",
   %5E -> "^") before putting it in .env - the raw decoded password is
   what belongs in SERVICENOW_PASSWORD, never the URL-encoded form.

3. Personal Developer Instances hibernate after inactivity. Log into the
   instance in a browser first to wake it up - REST calls do not wake it.

4. A 401 means bad credentials / password-reset-required / MFA blocking
   Basic Auth / account locked. A 403 means the account is missing the
   itil / rest_service role. This client raises these immediately with
   the response body shown, instead of masking them behind retries.

5. Quick standalone check - run this file directly:
       python -m common.servicenow_client
--------------------------------------------------------------------------
"""
import logging
import sys
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from common.models import Ticket
from config import ServiceNowSettings

logger = logging.getLogger(__name__)

INCIDENT_FIELDS = (
    "sys_id,number,sys_class_name,short_description,description,"
    "cmdb_ci,cmdb_ci.name,assignment_group,assignment_group.name,"
    "priority,state,close_notes"
)

SERVICE_REQUEST_FIELDS = (
    "sys_id,number,sys_class_name,short_description,description,"
    "cmdb_ci,cmdb_ci.name,assignment_group,assignment_group.name,"
    "requested_for,requested_for.email,opened_by,opened_by.email,"
    "priority,state,stage,active"
)

CATALOG_TASK_FIELDS = (
    "sys_id,number,sys_class_name,short_description,description,"
    "request_item,request_item.number,request_item.short_description,request_item.description,"
    "request_item.requested_for,request_item.requested_for.email,"
    "request_item.opened_by,request_item.opened_by.email,"
    "cmdb_ci,cmdb_ci.name,assignment_group,assignment_group.name,"
    "priority,state,active"
)


class ServiceNowAuthError(Exception):
    """Raised on 401/403 - credentials or role/permission problem. Never retried."""


class ServiceNowConnectionError(Exception):
    """Raised when the instance can't be reached at all (DNS, TLS, hibernating PDI, timeout)."""


def _normalize_instance_url(raw_url: str) -> str:
    """Strip any accidental path/query string down to just scheme + host."""
    if not raw_url:
        return raw_url
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        parsed = urlparse(f"https://{raw_url}")
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root != raw_url.rstrip("/"):
        logger.warning(
            "SERVICENOW_INSTANCE_URL contained a path/query string; normalized '%s' -> '%s'.",
            raw_url, root,
        )
    return root


class ServiceNowClient:
    """
    Minimal wrapper for the operations the AMS solution needs:
      - fetch new/unassigned incidents and service catalog tasks
      - enrich catalog tasks with parent RITM context
      - fetch a single ticket
      - update the assignment group (L2 routing) on an incident
      - add a work note or catalog task comment
      - resolve an assignment-group name to its sys_id
      - bulk-fetch historical/closed incidents for vector DB ingestion

    NOTE: username/password below are PLACEHOLDERS. Wire real credentials
    in via environment variables (SERVICENOW_USERNAME / SERVICENOW_PASSWORD)
    or your secrets manager of choice. Do not commit real credentials.
    """

    def __init__(self, settings: ServiceNowSettings):
        self.settings = settings
        self.base_url = _normalize_instance_url(settings.instance_url).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "SERVICENOW_INSTANCE_URL is not set. Set it to e.g. "
                "https://dev198124.service-now.com in your .env file."
            )

        # --- PLACEHOLDER CREDENTIALS: replace at runtime via env vars ---
        self.username = settings.username  # e.g. "REPLACE_ME_USERNAME"
        self.password = settings.password  # e.g. "REPLACE_ME_PASSWORD"
        # ------------------------------------------------------------------
        if self.username in ("", "REPLACE_ME_USERNAME") or self.password in ("", "REPLACE_ME_PASSWORD"):
            logger.warning(
                "SERVICENOW_USERNAME / SERVICENOW_PASSWORD still look like placeholders. "
                "Set real credentials in your .env file before running against a live instance."
            )

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ams-agentic-solution/1.0",
        })

        adapter = HTTPAdapter(max_retries=Retry(
            total=2, backoff_factor=1, status_forcelist=[502, 503, 504], allowed_methods=None,
        ))
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.verify_ssl = settings.verify_ssl
        self.timeout = settings.timeout_seconds

    # ------------------------------------------------------------------
    # Low-level request helper with clear diagnostics
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method, url, timeout=self.timeout, verify=self.verify_ssl, **kwargs
            )
        except requests.exceptions.SSLError as e:
            raise ServiceNowConnectionError(
                f"TLS/SSL error connecting to {self.base_url}. Original error: {e}"
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise ServiceNowConnectionError(
                f"Could not connect to {self.base_url}. If this is a PDI it may be hibernating - "
                f"log into it in a browser first. Original error: {e}"
            ) from e
        except requests.exceptions.Timeout as e:
            raise ServiceNowConnectionError(
                f"Timed out after {self.timeout}s connecting to {self.base_url}. Original error: {e}"
            ) from e

        if resp.status_code in (401, 403):
            raise ServiceNowAuthError(
                f"{resp.status_code} from {url}. "
                f"{'Check SERVICENOW_USERNAME/SERVICENOW_PASSWORD (and that it is not URL-encoded).' if resp.status_code == 401 else ''}"
                f"{'Check the account has the itil/rest_service role for this table.' if resp.status_code == 403 else ''} "
                f"Response body: {resp.text[:500]}"
            )
        if resp.status_code >= 400:
            logger.error("ServiceNow returned %s for %s %s: %s", resp.status_code, method, url, resp.text[:500])
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def get_new_incidents(self, extra_query: Optional[str] = None, limit: int = 50) -> List[Ticket]:
        """Fetch active incidents that still need L2 routing."""
        query = self.settings.incident_query_filter
        if extra_query:
            query = f"{query}^{extra_query}"
        params = {
            "sysparm_query": query,
            "sysparm_fields": INCIDENT_FIELDS,
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
        }
        resp = self._request("GET", "/api/now/table/incident", params=params)
        results = resp.json().get("result", [])
        return [self._to_ticket(r, table="incident") for r in results]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def get_new_service_requests(self, extra_query: Optional[str] = None, limit: int = 50) -> List[Ticket]:
        """Fetch open service catalog requested items (sc_req_item)."""
        query = self.settings.service_request_query_filter
        if extra_query:
            query = f"{query}^{extra_query}"
        params = {
            "sysparm_query": query,
            "sysparm_fields": SERVICE_REQUEST_FIELDS,
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
        }
        resp = self._request("GET", "/api/now/table/sc_req_item", params=params)
        results = resp.json().get("result", [])
        return [self._to_ticket(r, table="sc_req_item") for r in results]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def get_new_catalog_tasks(self, extra_query: Optional[str] = None, limit: int = 50) -> List[Ticket]:
        """Fetch open catalog tasks and enrich each task with its parent RITM details."""
        query = self.settings.service_request_task_query_filter
        if extra_query:
            query = f"{query}^{extra_query}"
        params = {
            "sysparm_query": query,
            "sysparm_fields": CATALOG_TASK_FIELDS,
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
        }
        resp = self._request("GET", "/api/now/table/sc_task", params=params)
        results = resp.json().get("result", [])

        tasks = []
        for raw in results:
            task = self._to_ticket(raw, table="sc_task")
            parent = self._fetch_parent_request_item(task.request_item_sys_id)
            if parent:
                task = self._merge_parent_request_item(task, parent)
            tasks.append(task)
        return tasks

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def get_catalog_tasks_for_request_item(self, request_item_sys_id: str, limit: int = 50) -> List[Ticket]:
        """Fetch catalog tasks that belong to a specific RITM."""
        if not request_item_sys_id:
            return []

        params = {
            "sysparm_query": f"request_item={request_item_sys_id}",
            "sysparm_fields": CATALOG_TASK_FIELDS,
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
        }
        resp = self._request("GET", "/api/now/table/sc_task", params=params)
        results = resp.json().get("result", [])
        return [self._to_ticket(raw, table="sc_task") for raw in results]

    def get_ticket(self, sys_id: str, table: str = "incident") -> Optional[Ticket]:
        try:
            resp = self._request("GET", f"/api/now/table/{table}/{sys_id}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        return self._to_ticket(resp.json().get("result", {}), table=table)

    def fetch_all_incidents_for_training(self, limit: int = 5000, resolved_only: bool = True) -> List[Dict]:
        """
        One-time bulk read of historical incidents used to seed the vector
        database. Only resolved/closed incidents are pulled by default,
        since these have a known-good assignment_group we can learn from.
        """
        query = "state=6^ORstate=7" if resolved_only else ""  # 6=Resolved, 7=Closed in default SNow data
        all_results: List[Dict] = []
        offset = 0
        page_size = min(limit, 500)
        while True:
            params = {
                "sysparm_query": query,
                "sysparm_fields": INCIDENT_FIELDS,
                "sysparm_limit": page_size,
                "sysparm_offset": offset,
                "sysparm_display_value": "true",  # want readable group/CI names for training text
            }
            resp = self._request("GET", "/api/now/table/incident", params=params)
            page = resp.json().get("result", [])
            if not page:
                break
            all_results.extend(page)
            offset += page_size
            if len(page) < page_size or len(all_results) >= limit:
                break
        return all_results

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def get_group_sys_id(self, group_name: str) -> Optional[str]:
        """Resolve a human-readable assignment group name to its sys_id."""
        params = {"sysparm_query": f"name={group_name}", "sysparm_fields": "sys_id,name", "sysparm_limit": 1}
        resp = self._request("GET", "/api/now/table/sys_user_group", params=params)
        results = resp.json().get("result", [])
        return results[0]["sys_id"] if results else None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def update_assignment_group(self, sys_id: str, group_name: str, table: str = "incident") -> bool:
        """Update the L2 assignment group on an incident via a REST PATCH call."""
        group_sys_id = self.get_group_sys_id(group_name)
        if not group_sys_id:
            logger.warning("Assignment group '%s' not found in sys_user_group; skipping update.", group_name)
            return False
        self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json={"assignment_group": group_sys_id})
        logger.info("Updated %s (%s) assignment_group -> %s", table, sys_id, group_name)
        return True

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def add_work_note(self, sys_id: str, note: str, table: str = "incident") -> bool:
        self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json={"work_notes": note})
        return True

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def add_comment(self, sys_id: str, comment: str, table: str = "sc_task") -> bool:
        self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json={"comments": comment})
        return True

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           retry=retry_if_exception_type((ServiceNowConnectionError,)), reraise=True)
    def close_catalog_task(self, sys_id: str, close_state: str = "3") -> bool:
        self._request(
            "PATCH",
            f"/api/now/table/sc_task/{sys_id}",
            json={"state": close_state},
        )
        return True

    # ------------------------------------------------------------------
    # Connectivity self-test
    # ------------------------------------------------------------------
    def test_connection(self) -> bool:
        try:
            resp = self._request("GET", "/api/now/table/sys_user",
                                  params={"sysparm_limit": 1, "sysparm_fields": "sys_id"})
            resp.json()
            logger.info("Connected to %s successfully as '%s'.", self.base_url, self.username)
            return True
        except ServiceNowAuthError as e:
            logger.error("Authentication/authorization failure: %s", e)
            return False
        except ServiceNowConnectionError as e:
            logger.error("Connection failure: %s", e)
            return False
        except Exception:
            logger.exception("Unexpected error while testing connection to %s", self.base_url)
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _fetch_parent_request_item(self, sys_id: Optional[str]) -> Optional[Ticket]:
        if not sys_id:
            return None
        try:
            return self.get_ticket(sys_id, table="sc_req_item")
        except Exception:
            logger.exception("Failed to fetch parent RITM %s for catalog task", sys_id)
            return None

    @staticmethod
    def _merge_parent_request_item(task: Ticket, parent: Ticket) -> Ticket:
        return task.model_copy(update={
            "request_item_sys_id": parent.sys_id or task.request_item_sys_id,
            "request_item_number": parent.number or task.request_item_number,
            "request_item_short_description": parent.short_description or task.request_item_short_description,
            "request_item_description": parent.description or task.request_item_description,
            "requested_for_email": parent.requested_for_email or task.requested_for_email,
            "opened_by_email": parent.opened_by_email or task.opened_by_email,
        })

    @staticmethod
    def _to_ticket(raw: Dict, table: str) -> Ticket:
        def flat(field):
            """ServiceNow reference fields come back as {'value':..,'display_value':..} or a plain string."""
            v = raw.get(field)
            if isinstance(v, dict):
                return v.get("value")
            return v

        def ref_attr(field, attribute):
            v = raw.get(field)
            return v.get(attribute) if isinstance(v, dict) else None

        cmdb_ci_name = raw.get("cmdb_ci.name") or ref_attr("cmdb_ci", "display_value")
        assignment_group_name = raw.get("assignment_group.name") or ref_attr("assignment_group", "display_value")
        request_item_sys_id = flat("request_item")
        request_item_number = raw.get("request_item.number")
        request_item_short_description = raw.get("request_item.short_description")
        request_item_description = raw.get("request_item.description")
        requested_for_email = (
            raw.get("requested_for.email")
            or raw.get("request_item.requested_for.email")
            or ref_attr("requested_for", "email")
        )
        opened_by_email = (
            raw.get("opened_by.email")
            or raw.get("request_item.opened_by.email")
            or ref_attr("opened_by", "email")
        )

        return Ticket(
            sys_id=raw.get("sys_id", ""),
            number=raw.get("number", ""),
            table=table,
            sys_class_name=raw.get("sys_class_name"),
            short_description=raw.get("short_description", "") or "",
            description=raw.get("description", "") or "",
            cmdb_ci=flat("cmdb_ci"),
            cmdb_ci_name=cmdb_ci_name,
            assignment_group=assignment_group_name,
            request_item_sys_id=request_item_sys_id,
            request_item_number=request_item_number,
            request_item_short_description=request_item_short_description,
            request_item_description=request_item_description,
            requested_for_email=requested_for_email,
            opened_by_email=opened_by_email,
            priority=raw.get("priority"),
            state=raw.get("state"),
            raw=raw,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from config import get_settings

    settings = get_settings()
    print(f"Testing connection to: {settings.servicenow.instance_url}")
    print(f"Using username: {settings.servicenow.username}")
    client = ServiceNowClient(settings.servicenow)
    ok = client.test_connection()
    if not ok:
        print("\nConnection test FAILED. See the log lines above for the specific reason.")
        sys.exit(1)
    print("\nConnection test PASSED.")
