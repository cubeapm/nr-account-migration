import argparse
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import library.localstore as store


NR_URLS = {
    "us": "https://api.newrelic.com/graphql",
    "eu": "https://api.eu.newrelic.com/graphql",
}


def resolve_region(cli_region: Optional[str]) -> str:
    region = (cli_region or os.environ.get("CUBE_MIGRATE_SRC_REGION") or "us").strip().lower()
    if region not in NR_URLS:
        raise ValueError(f"Unsupported region: {region}. Expected one of: {', '.join(sorted(NR_URLS.keys()))}")
    return region

# Keep receiver keys in the same order as index.js to preserve JSON output stability.
RECEIVER_KEYS = [
    "email_configs",
    "slack_configs",
    "pagerduty_configs",
    "jira_configs",
    "opsgenie_configs",
    "googlechat_configs",
    "webhook_configs",
]


def nr_query(api_key: str, query: str, *, region: str) -> Dict[str, Any]:
    req = Request(
        NR_URLS[region],
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"API-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"NerdGraph HTTP error: {e.code}. Body: {body}") from e
    except URLError as e:
        raise RuntimeError(f"NerdGraph network error: {e}") from e

    res = json.loads(raw)
    errors = res.get("errors")
    if errors and isinstance(errors, list) and len(errors) > 0:
        messages = " | ".join((e.get("message") or "").strip() for e in errors)
        raise RuntimeError(f"NerdGraph error: {messages}")
    if res.get("data") is None:
        raise RuntimeError("NerdGraph returned no data.")
    return res["data"]


def build_cursor_arg(cursor: Optional[str]) -> str:
    return f', cursor: "{cursor}"' if cursor else ""


def build_single_cursor_arg(cursor: Optional[str]) -> str:
    return f'(cursor: "{cursor}")' if cursor else ""


def fetch_all_pages(
    api_key: str,
    query_builder: Callable[[Optional[str]], str],
    extract_connection: Callable[[Dict[str, Any]], Dict[str, Any]],
    *,
    region: str,
) -> List[Dict[str, Any]]:
    cursor: Optional[str] = None
    entities: List[Dict[str, Any]] = []

    while True:
        data = nr_query(api_key, query_builder(cursor), region=region)
        connection = extract_connection(data) or {}
        entities.extend(connection.get("entities") or [])
        cursor = connection.get("nextCursor") or None
        if not cursor:
            break

    return entities


def get_workflows(api_key: str, account_id: int, *, region: str) -> List[Dict[str, Any]]:
    def query_builder(cursor: Optional[str]) -> str:
        return f"""
      {{
        actor {{
          account(id: {account_id}) {{
            aiWorkflows {{
              workflows(filters: {{}}{build_cursor_arg(cursor)}) {{
                entities {{
                  id
                  name
                  issuesFilter {{
                    predicates {{
                      attribute
                      values
                    }}
                  }}
                  destinationConfigurations {{
                    channelId
                    name
                    type
                    notificationTriggers
                  }}
                }}
                nextCursor
              }}
            }}
          }}
        }}
      }}
    """

    def extract_connection(data: Dict[str, Any]) -> Dict[str, Any]:
        return (
            (
                (
                    data.get("actor") or {}
                ).get("account") or {}
            ).get("aiWorkflows") or {}
        ).get("workflows") or {}

    return fetch_all_pages(api_key, query_builder, extract_connection, region=region)


def get_channels(api_key: str, account_id: int, *, region: str) -> List[Dict[str, Any]]:
    def query_builder(cursor: Optional[str]) -> str:
        return f"""
      {{
        actor {{
          account(id: {account_id}) {{
            aiNotifications {{
              channels{build_single_cursor_arg(cursor)} {{
                entities {{
                  id
                  name
                  type
                  destinationId
                  properties {{
                    key
                    value
                  }}
                }}
                nextCursor
              }}
            }}
          }}
        }}
      }}
    """

    def extract_connection(data: Dict[str, Any]) -> Dict[str, Any]:
        return (
            (
                (
                    data.get("actor") or {}
                ).get("account") or {}
            ).get("aiNotifications") or {}
        ).get("channels") or {}

    return fetch_all_pages(api_key, query_builder, extract_connection, region=region)


def get_destinations(api_key: str, account_id: int, *, region: str) -> List[Dict[str, Any]]:
    def query_builder(cursor: Optional[str]) -> str:
        return f"""
      {{
        actor {{
          account(id: {account_id}) {{
            aiNotifications {{
              destinations{build_single_cursor_arg(cursor)} {{
                entities {{
                  id
                  name
                  type
                  properties {{
                    key
                    value
                  }}
                }}
                nextCursor
              }}
            }}
          }}
        }}
      }}
    """

    def extract_connection(data: Dict[str, Any]) -> Dict[str, Any]:
        return (
            (
                (
                    data.get("actor") or {}
                ).get("account") or {}
            ).get("aiNotifications") or {}
        ).get("destinations") or {}

    return fetch_all_pages(api_key, query_builder, extract_connection, region=region)


def extract_properties(properties: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for p in properties or []:
        key = p.get("key")
        if key is None:
            continue
        obj[str(key)] = p.get("value")
    return obj


def is_email_notification(dc: Optional[Dict[str, Any]], channel: Optional[Dict[str, Any]]) -> bool:
    return (dc or {}).get("type") == "EMAIL" or (channel or {}).get("type") == "EMAIL"


def is_webhook_notification(
    dc: Optional[Dict[str, Any]], channel: Optional[Dict[str, Any]]
) -> bool:
    return (dc or {}).get("type") == "WEBHOOK" or (channel or {}).get("type") == "WEBHOOK"


def is_slack_notification(
    dc: Optional[Dict[str, Any]],
    channel: Optional[Dict[str, Any]],
    destination: Optional[Dict[str, Any]],
) -> bool:
    return (
        (dc or {}).get("type") == "SLACK"
        or (channel or {}).get("type") == "SLACK"
        or (destination or {}).get("type") == "SLACK"
    )


def extract_slack_channel_id(
    channel_props: Dict[str, Any], destination_props: Dict[str, Any]
) -> str:
    return (
        channel_props.get("channelId")
        or destination_props.get("channelId")
        or channel_props.get("channel")
        or destination_props.get("channel")
        or ""
    )


def notification_details(
    dc: Dict[str, Any],
    channel: Optional[Dict[str, Any]],
    destination: Optional[Dict[str, Any]],
    channel_props: Dict[str, Any],
    destination_props: Dict[str, Any],
) -> Dict[str, Any]:
    if is_email_notification(dc, channel):
        return {
            "email": destination_props.get("link_sent_email")
            or destination_props.get("email")
            or channel_props.get("email")
            or "",
            "subject": channel_props.get("subject")
            or channel_props.get("email_subject")
            or "",
        }

    if is_webhook_notification(dc, channel):
        return {
            "url": destination_props.get("url") or channel_props.get("url") or "",
            "payload": channel_props.get("payload")
            or channel_props.get("customPayload")
            or channel_props.get("custom_payload")
            or channel_props.get("body")
            or "",
        }

    if is_slack_notification(dc, channel, destination):
        slack_channel_id = extract_slack_channel_id(channel_props, destination_props)
        return {
            "slackChannelId": slack_channel_id,
            "webhookUrl": channel_props.get("url")
            or destination_props.get("url")
            or channel_props.get("webhookUrl")
            or destination_props.get("webhookUrl")
            or "",
            # Keep all raw fields so we can still consume unknown Slack keys.
            "slackRaw": {**destination_props, **channel_props},
        }

    # Default: carry through all destination+channel properties.
    return {**destination_props, **channel_props}


def destination_config_to_notification(
    dc: Dict[str, Any],
    channel_map: Dict[str, Dict[str, Any]],
    destination_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    channel = channel_map.get(dc.get("channelId"))
    destination = destination_map.get((channel or {}).get("destinationId"))
    channel_props = extract_properties((channel or {}).get("properties"))
    destination_props = extract_properties((destination or {}).get("properties"))

    return {
        "type": dc.get("type") or (channel or {}).get("type") or None,
        "channelName": dc.get("name") or (channel or {}).get("name") or None,
        "triggers": dc.get("notificationTriggers"),
        **notification_details(dc, channel, destination, channel_props, destination_props),
    }


def workflows_for_policy_id(
    workflows: List[Dict[str, Any]], policy_id: Any
) -> List[Dict[str, Any]]:
    policy_id_str = str(policy_id)
    matched: List[Dict[str, Any]] = []

    for wf in workflows:
        predicates = (((wf.get("issuesFilter") or {}).get("predicates")) or [])
        for p in predicates:
            if p.get("attribute") != "labels.policyIds":
                continue
            values = p.get("values") or []
            values_str = {str(v) for v in values}
            if policy_id_str in values_str:
                matched.append(wf)
                break

    return matched


def map_workflows_to_result(
    workflows: List[Dict[str, Any]],
    channel_map: Dict[str, Dict[str, Any]],
    destination_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for wf in workflows:
        notifications: List[Dict[str, Any]] = []
        for dc in wf.get("destinationConfigurations") or []:
            notifications.append(
                destination_config_to_notification(dc, channel_map, destination_map)
            )

        result.append(
            {
                "workflowId": wf.get("id"),
                "workflowName": wf.get("name"),
                "notifications": notifications,
            }
        )
    return result


def create_empty_receiver() -> Dict[str, List[Dict[str, Any]]]:
    return {k: [] for k in RECEIVER_KEYS}


def should_send_resolved(triggers: Optional[List[str]] = None) -> bool:
    return bool(triggers) and isinstance(triggers, list) and "CLOSED" in triggers


def build_receiver_groups(workflows_result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for index, workflow in enumerate(workflows_result):
        receiver = create_empty_receiver()

        for notification in workflow.get("notifications") or []:
            send_resolved = should_send_resolved(notification.get("triggers"))
            notif_type = notification.get("type")

            if notif_type == "EMAIL" and notification.get("email"):
                receiver["email_configs"].append(
                    {
                        "to": notification["email"],
                        "type": "email",
                        "send_resolved": send_resolved,
                        "cube_show_query": False,
                        "valid": True,
                    }
                )
                continue

            if notif_type == "SLACK" and notification.get("slackChannelId"):
                receiver["slack_configs"].append(
                    {
                        "channel": notification["slackChannelId"],
                        "type": "slack",
                        "send_resolved": send_resolved,
                        "cube_show_query": True,
                        "valid": True,
                    }
                )
                continue

            if notif_type == "WEBHOOK" and notification.get("url"):
                receiver["webhook_configs"].append(
                    {
                        "url": notification["url"],
                        "type": "webhook",
                        "send_resolved": send_resolved,
                        "cube_show_query": False,
                        "valid": True,
                        "headers": {},
                    }
                )

        groups.append(
            {"id": index + 1, "name": f"{workflow.get('workflowName')}", "receiver": receiver}
        )

    return groups


def merge_receiver_groups(receiver_groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged = create_empty_receiver()
    for group in receiver_groups:
        receiver = group.get("receiver") or {}
        for key in RECEIVER_KEYS:
            merged[key].extend(receiver.get(key) or [])

    # If we somehow ended up with zero configs, add a placeholder webhook.
    if not any(merged[key] for key in RECEIVER_KEYS):
        merged["webhook_configs"].append(
            {
                "url": "https://webhook.site/1234567890",
                "type": "webhook",
                "send_resolved": False,
                "cube_show_query": False,
                "valid": True,
                "headers": {},
            }
        )

    return merged


def dedupe_receiver_configs(receiver: Dict[str, Any]) -> Dict[str, Any]:
    deduped_receiver: Dict[str, Any] = {}
    for key, configs in (receiver or {}).items():
        seen = set()
        out: List[Dict[str, Any]] = []
        for config in configs or []:
            fingerprint = json.dumps(config, sort_keys=True, separators=(",", ":"))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(config)
        deduped_receiver[key] = out
    return deduped_receiver


def sql_escape(value: Any) -> str:
    return str(value).replace("'", "''")


def generate_receiver_group_insert_sql(
    per_policy_result: List[Dict[str, Any]], sql_account_id: int
) -> str:
    lines: List[str] = [
        "-- Generated by index.py",
        "-- Inserts one alert_receiver_group per policyId",
        "BEGIN;",
    ]

    for item in per_policy_result:
        policy_id = item.get("policyId")
        policy_name = item.get("policyName")
        receiver = item.get("receiver")

        if not policy_name:
            raise ValueError(f"Missing policyName when generating SQL for policyId={policy_id}")

        name = policy_name
        receiver_json = json.dumps(receiver, separators=(",", ":"), ensure_ascii=False)

        lines.append(
            "INSERT INTO alert_receiver_groups "
            f"(account_id, name, status, receiver, created_at, updated_at) VALUES "
            f"({sql_account_id}, '{sql_escape(name)}', 'ACTIVE', '{sql_escape(receiver_json)}'::jsonb, NOW(), NOW());"
        )

    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch New Relic NerdGraph AI workflow notification settings and generate Cube receiver-group SQL."
    )
    parser.add_argument(
        "--api-key",
        required=False,
        help="New Relic NerdGraph API key (sent as API-Key header). "
             "Defaults to env var CUBE_MIGRATE_SRC_KEY.",
    )
    parser.add_argument(
        "--account-id",
        required=False,
        type=int,
        help="New Relic account id for NerdGraph queries. Defaults to env var CUBE_MIGRATE_SRC_ACCOUNT.",
    )
    parser.add_argument(
        "--region",
        required=False,
        choices=sorted(NR_URLS.keys()),
        help="New Relic region for NerdGraph (us|eu). Defaults to env var CUBE_MIGRATE_SRC_REGION or 'us'.",
    )
    parser.add_argument(
        "--alert-policies",
        required=False,
        default=None,
        help="Path to alert_policies.json produced by store_policies.py. "
             "Defaults to db/<account-id>/alert_policies/alert_policies.json. "
             "If provided, overrides the default.",
    )

    parser.add_argument(
        "--workflows-json",
        default=None,
        help="Output path for workflows JSON (defaults to db/<account-id>/receiver_groups/nr_workflows.json).",
    )
    parser.add_argument(
        "--channels-json",
        default=None,
        help="Output path for channels JSON (defaults to db/<account-id>/receiver_groups/nr_channels.json).",
    )
    parser.add_argument(
        "--destinations-json",
        default=None,
        help="Output path for destinations JSON (defaults to db/<account-id>/receiver_groups/nr_destinations.json).",
    )
    parser.add_argument(
        "--receiver-groups-sql",
        default=None,
        help="Output path for receiver-group SQL (defaults to db/<account-id>/receiver_groups/receiver_groups_insert.sql).",
    )

    parser.add_argument(
        "--sql-account-id",
        type=int,
        default=1,
        help="account_id to embed in the SQL (defaults to 1 to match index.js).",
    )

    args = parser.parse_args()
    sql_account_id = args.sql_account_id

    api_key = (args.api_key or os.environ.get("CUBE_MIGRATE_SRC_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "Missing NerdGraph API key. Provide --api-key or set env var CUBE_MIGRATE_SRC_KEY."
        )

    account_id = args.account_id
    if account_id is None:
        env_account = (os.environ.get("CUBE_MIGRATE_SRC_ACCOUNT") or "").strip()
        if env_account:
            try:
                account_id = int(env_account)
            except ValueError as e:
                raise ValueError(
                    f"Invalid CUBE_MIGRATE_SRC_ACCOUNT value: {env_account!r}. Expected an integer."
                ) from e
    if account_id is None:
        raise ValueError(
            "Missing account id. Provide --account-id or set env var CUBE_MIGRATE_SRC_ACCOUNT."
        )

    region = resolve_region(args.region)

    account_dir = Path("db") / str(account_id)
    receiver_groups_dir = account_dir / "receiver_groups"
    receiver_groups_dir.mkdir(mode=0o777, parents=True, exist_ok=True)

    alert_policies_path = (
        Path(args.alert_policies)
        if args.alert_policies
        else account_dir / store.ALERT_POLICIES_DIR / store.ALERT_POLICIES_FILE
    )
    if not alert_policies_path.exists():
        raise FileNotFoundError(
            f"Alert policies file not found: {alert_policies_path}. "
            f"Run store_policies.py first (or pass --alert-policies)."
        )

    workflows_json_path = (
        Path(args.workflows_json)
        if args.workflows_json
        else receiver_groups_dir / "nr_workflows.json"
    )
    channels_json_path = (
        Path(args.channels_json)
        if args.channels_json
        else receiver_groups_dir / "nr_channels.json"
    )
    destinations_json_path = (
        Path(args.destinations_json)
        if args.destinations_json
        else receiver_groups_dir / "nr_destinations.json"
    )
    receiver_groups_sql_path = (
        Path(args.receiver_groups_sql)
        if args.receiver_groups_sql
        else receiver_groups_dir / "receiver_groups_insert.sql"
    )

    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_workflows = ex.submit(get_workflows, api_key, account_id, region=region)
        fut_channels = ex.submit(get_channels, api_key, account_id, region=region)
        fut_destinations = ex.submit(get_destinations, api_key, account_id, region=region)
        workflows = fut_workflows.result()
        channels = fut_channels.result()
        destinations = fut_destinations.result()

    store.save_json(workflows_json_path.parent, workflows_json_path.name, workflows)
    store.save_json(channels_json_path.parent, channels_json_path.name, channels)
    store.save_json(destinations_json_path.parent, destinations_json_path.name, destinations)

    print(
        f"✅ Wrote {workflows_json_path}, {channels_json_path}, {destinations_json_path}"
    )

    channel_map = {str(c.get("id")): c for c in channels}
    destination_map = {str(d.get("id")): d for d in destinations}

    with open(alert_policies_path, "r", encoding="utf-8") as f:
        alert_policies = json.load(f)

    policies = alert_policies.get("policies") or []

    result: List[Dict[str, Any]] = []
    for policy in policies:
        policy_id = policy.get("id")
        policy_name = policy.get("name")
        if not policy_name:
            raise ValueError(f"Missing policy.name in alert_policies.json for policyId={policy_id}")

        matched_workflows = workflows_for_policy_id(workflows, policy_id)
        workflows_result = map_workflows_to_result(matched_workflows, channel_map, destination_map)
        workflow_receiver_groups = build_receiver_groups(workflows_result)
        merged_receiver = dedupe_receiver_configs(merge_receiver_groups(workflow_receiver_groups))

        result.append(
            {
                "policyId": policy_id,
                "policyName": policy_name,
                "workflows": workflows_result,
                "receiverGroup": {"name": policy_name, "receiver": merged_receiver},
            }
        )

    receiver_group_inserts = [
        {"policyId": item["policyId"], "policyName": item["policyName"], "receiver": item["receiverGroup"]["receiver"]}
        for item in result
    ]

    sql_content = generate_receiver_group_insert_sql(receiver_group_inserts, sql_account_id=sql_account_id)

    store.create_file(receiver_groups_sql_path)
    with open(receiver_groups_sql_path, "w", encoding="utf-8") as f:
        f.write(sql_content)

    print(f"✅ Generated {receiver_groups_sql_path}")


if __name__ == "__main__":
    main()

