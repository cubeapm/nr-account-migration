# Utility to migrate alerts from New Relic to CubeAPM

## Prepare

Set environment variables for convenience

```
export CUBE_MIGRATE_SRC_ACCOUNT=<new_relic_account_id>
export CUBE_MIGRATE_SRC_KEY=<new_relic_user_api_key>
# region can be us or eu
export CUBE_MIGRATE_SRC_REGION=<new_relic_account_region>
```

## Download Entities

In alerts, we deal with two types of entities, `Applications` and `Key Transactions`. Alert queries may refer to them by `entityGuids`, so we need a way to map the guids to corresponding names. Here we fetch the entity details mainly to use for mapping Application entityGuids to Application names.

```
python3 cubeapm_fetchentities.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION --toFile ${CUBE_MIGRATE_SRC_ACCOUNT}_entities.json --synthetics --apm --browser --dashboards --infrahost --infraint --mobile --lambda --workload

# output: output/<account_id>_entities.json
```

Note: `cubeapm_fetchentities.py` is a copy of `fetchentities.py` with small modification to save full entities data instead of just entity names.

## Download Channels

```
python3 fetchchannels.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --region $CUBE_MIGRATE_SRC_REGION

# output: db/<account_id>/alert_policies/alert_channels.json
```

## Download Alert Policies

```
python3 store_policies.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION

# output: db/<account_id>/alert_policies/alert_policies.json
# output: output/<account_id>_policies.csv
```

## Download Alert Conditions

```
python3 cubeapm.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION --fromFile output/${CUBE_MIGRATE_SRC_ACCOUNT}_policies.csv

# output: db/<account_id>/alert_policies/alert_conditions.json
```

## Extend Entities

```
python3 cubeapm1.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --sourceApiKey $CUBE_MIGRATE_SRC_KEY --sourceRegion $CUBE_MIGRATE_SRC_REGION

# output: output/<account_id>_entities_extended.json
```

## Generate SQL

```
python3 cubeapm2.py --sourceAccount $CUBE_MIGRATE_SRC_ACCOUNT --compat <legacy_or_modern> --mode <mysql_or_postgresql>

# output: stdout
```

### Analyze New Relic Queries

```
cat db/$CUBE_MIGRATE_SRC_ACCOUNT/alert_policies/alert_conditions.json | jq '.nrql | .[] | .nrql.query' | sort > db/$CUBE_MIGRATE_SRC_ACCOUNT/alert_policies/alert_queries.txt
```
