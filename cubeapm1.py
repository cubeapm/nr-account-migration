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


# def debug_entity_matching(entity_id, entities, entities_extended):
#     """Debug function to help understand entity matching issues"""
#     logger.info(f"=== DEBUG ENTITY MATCHING FOR {entity_id} ===")
    
#     # Clean entity ID
#     clean_entity_id = entity_id
#     if entity_id.startswith('entity-'):
#         clean_entity_id = entity_id.replace('entity-', '')
#         logger.info(f"Original ID: {entity_id}, Clean ID: {clean_entity_id}")
    
#     # Check in base entities
#     logger.info(f"Checking base entities ({len(entities)} total)...")
#     base_matches = []
#     for i, entity in enumerate(entities[:10]):  # Check first 10
#         if (entity.get('id') == entity_id or entity.get('guid') == entity_id or
#             entity.get('id') == clean_entity_id or entity.get('guid') == clean_entity_id):
#             base_matches.append(entity)
#             logger.info(f"✓ Base entity match #{i}: {entity}")
    
#     if not base_matches:
#         logger.info("No matches found in base entities")
    
#     # Check in extended entities
#     logger.info(f"Checking extended entities ({len(entities_extended)} total)...")
#     ext_matches = []
#     for i, entity in enumerate(entities_extended[:20]):  # Check first 20
#         ext_app_id = entity.get('applicationId')
#         ext_guid = entity.get('guid')
#         ext_name = entity.get('name')
        
#         if (ext_app_id and (str(ext_app_id) == str(entity_id) or str(ext_app_id) == str(clean_entity_id))):
#             ext_matches.append(('applicationId', entity))
#             logger.info(f"✓ Extended entity match by applicationId #{i}: {entity}")
#         elif (ext_guid and (ext_guid == entity_id or ext_guid == clean_entity_id)):
#             ext_matches.append(('guid', entity))
#             logger.info(f"✓ Extended entity match by guid #{i}: {entity}")
#         elif (ext_name and clean_entity_id in str(ext_name)):
#             ext_matches.append(('name', entity))
#             logger.info(f"✓ Extended entity match by name #{i}: {entity}")
    
#     if not ext_matches:
#         logger.info("No matches found in extended entities")
#         # Show some sample entities for debugging
#         logger.info("Sample extended entities:")
#         for i, entity in enumerate(entities_extended[:5]):
#             logger.info(f"  #{i}: applicationId={entity.get('applicationId')}, guid={entity.get('guid')}, name={entity.get('name')}")
    
#     logger.info(f"=== END DEBUG ENTITY MATCHING ===")
#     return base_matches, ext_matches


def migrate(
    src_acct_id: int,
    src_region: str,
    src_api_key: str,
):
    # Load both entities files - the base one and the extended one
    entities = store.load_json_from_file('output', '%s_entities.json' % str(src_acct_id))
    # entities_extended = store.load_json_from_file('output', '%s_entities_extended.json' % str(src_acct_id))
    alert_conditions = store.load_json_file(src_acct_id, store.ALERT_POLICIES_DIR, 'alert_conditions.json')
    
    logger.info(f"Loaded {len(entities)} base entities")
    # logger.info(f"Loaded {len(entities_extended)} extended entities")

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

    # # Process App conditions
    # logger.info(f"=== PROCESSING APP CONDITIONS ===")
    # logger.info(f"Total app conditions: {len(alert_conditions['app'])}")
    
    for i, condition in enumerate(alert_conditions['app']):
        # logger.info(f"--- Processing app condition {i+1}/{len(alert_conditions['app'])} ---")
        # logger.info(f"Condition: {condition.get('name', 'Unknown')}")
        # logger.info(f"Condition type: {condition.get('type', 'Unknown')}")
        # logger.info(f"Full condition: {json.dumps(condition, indent=2)}")
        
        entity_ids = condition.get('entities', [])
        logger.info(f"Entity IDs: {entity_ids}")
        
        if not entity_ids:
            logger.info("No entity IDs found, skipping condition")
            continue
        
        for j, entity_id in enumerate(entity_ids):
            # logger.info(f"--- Processing entity {j+1}/{len(entity_ids)}: {entity_id} ---")
            
            # # Debug entity matching first
            # debug_entity_matching(entity_id, entities, entities_extended)
            
            # Handle entity IDs with 'entity-' prefix
            # clean_entity_id = entity_id
            # if entity_id.startswith('entity-'):
            #     clean_entity_id = entity_id.replace('entity-', '')
            #     logger.info(f"Cleaned entity ID: {clean_entity_id} (removed 'entity-' prefix)")
            
            # First check if entity already exists in our base entities list
            existing_entities = [e for e in entities if e.get('id') == entity_id or e.get('guid') == entity_id or 
                               e.get('applicationId') == entity_id]
            if existing_entities:
                logger.info(f"Entity {entity_id} already exists in base entities list, skipping")
                continue
            
            # Check if entity exists in extended entities file
            # logger.info(f"Searching for entity {entity_id} (clean ID: {clean_entity_id}) in extended entities file...")
            # found_entity = None
            
            # for ext_entity in entities_extended:
            #     # Check if this entity matches by applicationId (which matches the entity_id from alert conditions)
            #     # Handle both string and integer types for applicationId
            #     ext_app_id = ext_entity.get('applicationId')
            #     if ext_app_id is not None:
            #         if str(ext_app_id) == str(entity_id) or str(ext_app_id) == str(clean_entity_id):
            #             logger.info(f"✓ Found entity {entity_id} in extended entities file by applicationId")
            #             found_entity = ext_entity
            #             break
                
            #     # Also check by guid as fallback
            #     elif ext_entity.get('guid') == entity_id or ext_entity.get('guid') == clean_entity_id:
            #         logger.info(f"✓ Found entity {entity_id} in extended entities file by guid")
            #         found_entity = ext_entity
            #         break
                
            #     # Check if the entity name contains the clean entity ID
            #     elif ext_entity.get('name') and clean_entity_id in str(ext_entity.get('name')):
            #         logger.info(f"✓ Found entity {entity_id} in extended entities file by name match")
            #         found_entity = ext_entity
            #         break
            
    #         if found_entity:
    #             # Transform the extended entity to match the expected format
    #             transformed_entity = {
    #                 'id': found_entity.get('applicationId') or clean_entity_id,
    #                 'guid': found_entity.get('guid'),
    #                 'name': found_entity.get('name'),
    #                 'type': found_entity.get('type'),
    #                 'domain': found_entity.get('entityType', '').replace('_ENTITY', '').lower(),
    #                 'accountId': found_entity.get('accountId'),
    #                 'language': found_entity.get('language'),
    #                 'tags': found_entity.get('tags', [])
    #             }
                
    #             # Also add the original entity ID for reference
    #             transformed_entity['original_entity_id'] = entity_id
    #             if entity_id != clean_entity_id:
    #                 transformed_entity['clean_entity_id'] = clean_entity_id
                
    #             logger.info(f"✓ Transformed entity: {transformed_entity}")
    #             entities.append(transformed_entity)
    #             logger.info(f"✓ Entity added to base entities list")
                
    #             # Verify the entity was added correctly
    #             logger.info(f"✓ Verification: Entity list now has {len(entities)} entities")
    #             added_entity = [e for e in entities if e.get('id') == transformed_entity['id'] or e.get('guid') == transformed_entity['guid']]
    #             if added_entity:
    #                 logger.info(f"✓ Entity successfully found in list: {added_entity[0]}")
    #             else:
    #                 logger.warning(f"⚠️ Entity not found in list after adding - this indicates a problem")
    #         else:
    #             logger.warning(f"⚠️ Entity {entity_id} not found in extended entities file")
    #             logger.warning(f"Available applicationIds: {[e.get('applicationId') for e in entities_extended[:10]]}...")
                
    #             # Fallback: try to fetch via API (but this should rarely happen)
    #             logger.info(f"Attempting API fallback for entity {entity_id}...")
    #             entity_type = utils.get_entity_type(condition)
    #             res = get_entity_by_id(clean_entity_id, entity_type, src_api_key, src_region)
    #             logger.info(f"API fallback result: {res}")
                
    #             if res['entityFound'] and res['entity']:
    #                 entities.append(res['entity'])
    #                 logger.info(f"✓ Entity added via API fallback")
    #             else:
    #                 logger.error(f"❌ Entity {entity_id} not found via API either")
            
    #         logger.info(f"=== END APP CONDITION PROCESSING ===")
    
    # logger.info(f"=== FINISHED PROCESSING APP CONDITIONS ===")
    # logger.info(f"Total entities after processing: {len(entities)}")

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
