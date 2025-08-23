import os
import re
import json
import requests
import argparse
import library.localstore as store
import library.migrationlogger as logger
from library.clients.endpoints import Endpoints
import library.utils as utils
import library.clients.entityclient as ec
from pathlib import Path


logger = logger.get_logger(os.path.basename(__file__))


guidRegEx = r"(?P<idType>appId|entity\.guid)\s*(?:=\s*(?P<guid>\d+|(?:'|\")[^'\"]+(?:'|\"))|IN\s*\((?P<guids>[^)]+)\))"


def migrate(
    src_acct_id: int,
    src_region: str,
    src_api_key: str,
):
    entities = store.load_json_from_file('output', '%s_entities.json' % str(src_acct_id))
    alert_conditions = store.load_json_file(src_acct_id, store.ALERT_POLICIES_DIR, 'alert_conditions.json')

    # Process NRQL conditions
    for condition in alert_conditions['nrql']:
        res = re.search(guidRegEx, condition['nrql']['query'], flags=re.IGNORECASE)
        if not res:
            continue
        
        groupdict = res.groupdict()

        guids = [groupdict.get('guid')] if groupdict.get('guid') else []
        if groupdict.get('guids'):
            guids = groupdict.get('guids').split(',')
        guids = [x.strip().strip("'\"") for x in guids]
        if not guids:
            raise ValueError("empty guids")
        
        for guid in guids:
            if any(e.get('guid') == guid for e in entities):
                # already exists. skip.
                continue
            
            # not found. fetch.
            idType = groupdict.get('idType')

            if idType == 'appId':
                logger.error("AppId not found for guid %s" % guid)
            elif idType == 'entity.guid':
                res = get_key_transaction_entity(guid, src_api_key, src_region)
                if res['entityFound'] and res['entity']:
                    entities.append(res['entity'])
                else:
                    logger.error("key transaction not found for guid %s" % guid) 
            else:
                raise ValueError("unhandled idType " + idType)

    # Process App conditions
    logger.info(f"=== PROCESSING APP CONDITIONS ===")
    logger.info(f"Total app conditions: {len(alert_conditions['app'])}")
    
    for i, condition in enumerate(alert_conditions['app']):
        logger.info(f"--- Processing app condition {i+1}/{len(alert_conditions['app'])} ---")
        logger.info(f"Condition: {condition.get('name', 'Unknown')}")
        logger.info(f"Condition type: {condition.get('type', 'Unknown')}")
        logger.info(f"Full condition: {json.dumps(condition, indent=2)}")
        
        entity_ids = condition.get('entities', [])
        logger.info(f"Entity IDs: {entity_ids}")
        
        if not entity_ids:
            logger.info("No entity IDs found, skipping condition")
            continue
        
        for j, entity_id in enumerate(entity_ids):
            logger.info(f"--- Processing entity {j+1}/{len(entity_ids)}: {entity_id} ---")
            
            # Check if entity already exists in our list
            existing_entities = [e for e in entities if e.get('id') == entity_id or e.get('guid') == entity_id]
            if existing_entities:
                logger.info(f"Entity {entity_id} already exists in list, skipping")
                continue
            
            # not found. fetch.
            entity_type = utils.get_entity_type(condition)
            logger.info(f"=== PROCESSING APP CONDITION ===")
            logger.info(f"Condition: {condition.get('name', 'Unknown')}")
            logger.info(f"Entity ID: {entity_id}")
            logger.info(f"Entity Type: {entity_type}")
            logger.info(f"API Key: {src_api_key[:10]}..." if src_api_key else "None")
            logger.info(f"Region: {src_region}")
            
            res = get_entity_by_id(entity_id, entity_type, src_api_key, src_region)
            logger.info(f"Entity lookup result: {res}")
            
            if res['entityFound'] and res['entity']:
                entities.append(res['entity'])
                logger.info(f"✓ Entity added to list")
            else:
                logger.error("entity not found for id %s" % entity_id)
                logger.error(f"Full result: {res}")
            
            logger.info(f"=== END APP CONDITION PROCESSING ===")
    
    logger.info(f"=== FINISHED PROCESSING APP CONDITIONS ===")
    logger.info(f"Total entities after processing: {len(entities)}")

    save_entities_extended(str(src_acct_id), entities)


def save_entities_extended(account_id, entities):
    store.save_json(Path("output"), '%s_entities_extended.json' % account_id, entities)


def get_key_transaction_entity(guid, src_api_key, src_region):
    data = {
        'variables': '',
        'query': '''{
  actor {
    entity(guid: "'''+ guid +'''") {
      domain
      entityType
      guid
      name
      type
      ... on KeyTransactionEntity {
        application {
          entity {
            domain
            entityType
            guid
            name
            type
          }
        }
        metricName
      }
    }
  }
}'''
    }

    response = requests.post(Endpoints.of(src_region).GRAPHQL_URL, json=data, headers={
        'API-Key': src_api_key
    })

    result = {'entityFound': False}
    result['status'] = response.status_code
    if response.text:
        response_json = response.json()
        if 'errors' in response_json:
            if response.text:
                result['error'] = response_json['errors']
            logger.error(result)
        else:
            result['entity'] = response_json['data']['actor']['entity']
            result['entityFound'] = True
    else:
        logger.warn('No response for this query response received ' + str(response))
    logger.info('entity match result : ' + str(result))
    return result


def get_entity_by_id(entity_id, entity_type, src_api_key, src_region):
    """Fetch entity by ID and type using the entity client"""
    result = ec.get_entity(src_api_key, entity_type, entity_id, src_region)
    return result


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description='Migrate Alert Policies, Alert Conditions, and channels'
    )
    return configure_parser(parser)


def configure_parser(
    parser: argparse.ArgumentParser,
    is_standalone: bool = True
):
    parser.add_argument(
        '--sourceAccount',
        '--source_account_id',
        nargs=1,
        type=int, 
        required=is_standalone,
        help='Source accountId',
        dest='source_account_id'
    )
    parser.add_argument(
        '--sourceRegion',
        '--source_region',
        nargs=1,
        type=str,
        required=False,
        help='Source Account Region us(default) or eu',
        dest='source_region'
    )
    parser.add_argument(
        '--sourceApiKey',
        '--source_api_key',
        nargs=1,
        type=str,
        required=False,
        help='Source account API Key or set environment variable ENV_SOURCE_API_KEY',
        dest='source_api_key'
    )
    return parser

def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    source_api_key = utils.ensure_source_api_key(args)
    if not source_api_key:
        utils.error_and_exit('source_api_key', 'ENV_SOURCE_API_KEY')
    sourceRegion = utils.ensure_source_region(args)

    migrate(
        args.source_account_id[0],
        sourceRegion,
        source_api_key
    )

if __name__ == '__main__':
    main()
