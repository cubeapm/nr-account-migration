# Utility to migrate dashboards from New Relic to CubeAPM

## Prepare

Set environment variables for convenience

```bash
export CUBE_MIGRATE_SRC_ACCOUNT=<new_relic_account_id>
export CUBE_MIGRATE_SRC_KEY=<new_relic_user_api_key>
# region can be us or eu
export CUBE_MIGRATE_SRC_REGION=<new_relic_account_region>
```

## Download Entities

In dashboards, we deal with various types of entities that widgets may reference. We need to map entity GUIDs to corresponding names for proper translation.

```bash
python3 cubeapm_fetchentities.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION --toFile ${CUBE_MIGRATE_SRC_ACCOUNT}_entities.json --synthetics --apm --browser --dashboards --infrahost --infraint --mobile --lambda --workload

# output: output/<account_id>_entities.json
```

## List Available Dashboards

First, list all available dashboards to create the dashboard names file:

```bash
python3 cubeapm_listdashboards.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION

# output: dashboard_names.txt
```

## Download Dashboards

```bash
python3 cubeapm_fetchdashboards.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION --fromFile dashboard_names.txt

# output: db/<account_id>/dashboards/dashboards.json
# output: output/<account_id>_dashboards.csv
```

## Download Dashboard Widgets

```bash
python3 cubeapm_fetchwidgets.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION --fromFile output/${CUBE_MIGRATE_SRC_ACCOUNT}_dashboards.csv

# output: db/<account_id>/dashboards/dashboard_widgets.json
```

## Extend Entities

```bash
python3 cubeapm_dashboard1.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION

# output: output/<account_id>_entities_extended.json
```

## Generate CubeAPM Dashboard Configuration

```bash
python3 cubeapm_dashboard2.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --mode <mysql_or_postgresql>

# output: output/<account_id>_cubeapm_dashboards.json
```

### Analyze New Relic Dashboard Queries

```bash
cat db/$CUBE_MIGRATE_SRC_ACCOUNT/dashboards/dashboard_widgets.json | jq '.widgets | .[] | .rawConfiguration.nrqlQueries | .[] | .query' | sort > db/$CUBE_MIGRATE_SRC_ACCOUNT/dashboards/dashboard_queries.txt
```

## Dashboard Migration Workflow

1. **Fetch entities** - Get all entity mappings needed for widget translation
2. **List dashboards** - Get all available dashboard names from New Relic
3. **Download dashboards** - Get dashboard definitions and metadata
4. **Download widgets** - Get all widget configurations with NRQL queries
5. **Extend entities** - Find any missing entities referenced in widgets
6. **Generate CubeAPM config** - Convert New Relic dashboards to CubeAPM format

## Supported Widget Types

- **NRQL widgets** - Line charts, bar charts, pie charts, tables
- **Metric widgets** - Single value displays
- **Markdown widgets** - Text and documentation
- **Billboard widgets** - Key performance indicators

## CubeAPM Dashboard Format

The generated CubeAPM dashboard configuration includes:
- Dashboard metadata (name, description, layout)
- Widget configurations with translated queries
- Entity mappings for service names
- Chart configurations compatible with CubeAPM 