#!/usr/bin/env python3
"""
Entity Resolution Fix Script

This script helps debug and fix entity resolution issues by:
1. Analyzing entity data files
2. Checking alert conditions for missing entities
3. Attempting to fetch missing entities from New Relic
4. Creating a corrected entity mapping
"""

import os
import json
import re
import argparse
import library.localstore as store
import library.migrationlogger as logger
import library.clients.entityclient as ec
import library.utils as utils
from pathlib import Path

logger = logger.get_logger(os.path.basename(__file__))

def extract_entity_ids_from_prometheus_query(query):
    """Extract entity IDs from Prometheus query expressions"""
    # Pattern to match entity-XXXXXXX in Prometheus queries
    pattern = r'entity\\-(\d+)'
    matches = re.findall(pattern, query)
    return [f"entity-{match}" for match in matches]

def analyze_entity_files(account_id):
    """Analyze entity files to understand the current state"""
    logger.info(f"=== ANALYZING ENTITY FILES FOR ACCOUNT {account_id} ===")
    
    # Load entity files
    try:
        entities = store.load_json_from_file('output', f'{account_id}_entities.json')
        entities_extended = store.load_json_from_file('output', f'{account_id}_entities_extended.json')
        logger.info(f"✓ Loaded {len(entities)} base entities")
        logger.info(f"✓ Loaded {len(entities_extended)} extended entities")
    except Exception as e:
        logger.error(f"❌ Failed to load entity files: {e}")
        return None, None
    
    # Analyze entity types and IDs
    entity_types = {}
    entity_ids = []
    
    for entity in entities_extended:
        entity_type = entity.get('entityType', 'UNKNOWN')
        entity_types[entity_type] = entity_types.get(entity_type, 0) + 1
        
        # Collect all possible identifiers
        app_id = entity.get('applicationId')
        guid = entity.get('guid')
        name = entity.get('name')
        
        if app_id:
            entity_ids.append(str(app_id))
        if guid:
            entity_ids.append(guid)
        if name:
            entity_ids.append(name)
    
    logger.info(f"Entity types found: {entity_types}")
    logger.info(f"Total unique identifiers: {len(set(entity_ids))}")
    
    return entities, entities_extended

def find_missing_entities(required_entity_ids, entities, entities_extended):
    """Find which entities are missing from the entity files"""
    logger.info(f"=== FINDING MISSING ENTITIES ===")
    
    missing_entities = []
    found_entities = []
    
    for entity_id in required_entity_ids:
        logger.info(f"Checking entity: {entity_id}")
        
        # Clean entity ID
        clean_id = entity_id.replace('entity-', '') if entity_id.startswith('entity-') else entity_id
        
        # Check in base entities
        found_in_base = any(
            e.get('id') == entity_id or e.get('guid') == entity_id or
            e.get('id') == clean_id or e.get('guid') == clean_id
            for e in entities
        )
        
        # Check in extended entities
        found_in_ext = any(
            e.get('applicationId') == clean_id or e.get('guid') == clean_id or
            str(e.get('applicationId')) == str(clean_id) or str(e.get('guid')) == str(clean_id)
            for e in entities_extended
        )
        
        if found_in_base or found_in_ext:
            logger.info(f"✓ Entity {entity_id} found")
            found_entities.append(entity_id)
        else:
            logger.warning(f"⚠️ Entity {entity_id} NOT FOUND")
            missing_entities.append(entity_id)
    
    logger.info(f"Found entities: {len(found_entities)}")
    logger.info(f"Missing entities: {len(missing_entities)}")
    
    return missing_entities, found_entities

def fetch_missing_entities(missing_entity_ids, api_key, region):
    """Attempt to fetch missing entities from New Relic"""
    logger.info(f"=== FETCHING MISSING ENTITIES ===")
    
    fetched_entities = []
    
    for entity_id in missing_entity_ids:
        clean_id = entity_id.replace('entity-', '') if entity_id.startswith('entity-') else entity_id
        logger.info(f"Fetching entity {entity_id} (clean ID: {clean_id})")
        
        # Try to fetch as APM application first
        try:
            result = ec.get_entity(api_key, ec.APM_APP, clean_id, region)
            if result.get('entityFound'):
                logger.info(f"✓ Successfully fetched entity {entity_id} as APM_APP")
                fetched_entities.append({
                    'original_id': entity_id,
                    'clean_id': clean_id,
                    'entity_data': result['entity'],
                    'entity_type': 'APM_APP'
                })
                continue
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch {entity_id} as APM_APP: {e}")
        
        # Try to fetch as browser application
        try:
            result = ec.get_entity(api_key, ec.BROWSER_APP, clean_id, region)
            if result.get('entityFound'):
                logger.info(f"✓ Successfully fetched entity {entity_id} as BROWSER_APP")
                fetched_entities.append({
                    'original_id': entity_id,
                    'clean_id': clean_id,
                    'entity_data': result['entity'],
                    'entity_type': 'BROWSER_APP'
                })
                continue
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch {entity_id} as BROWSER_APP: {e}")
        
        # Try to fetch as mobile application
        try:
            result = ec.get_entity(api_key, ec.MOBILE_APP, clean_id, region)
            if result.get('entityFound'):
                logger.info(f"✓ Successfully fetched entity {entity_id} as MOBILE_APP")
                fetched_entities.append({
                    'original_id': entity_id,
                    'clean_id': clean_id,
                    'entity_data': result['entity'],
                    'entity_type': 'MOBILE_APP'
                })
                continue
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch {entity_id} as MOBILE_APP: {e}")
        
        logger.error(f"❌ Failed to fetch entity {entity_id} with any entity type")
    
    return fetched_entities

def create_entity_mapping_file(account_id, missing_entities, fetched_entities):
    """Create a mapping file to help resolve entity references"""
    logger.info(f"=== CREATING ENTITY MAPPING FILE ===")
    
    mapping = {
        'account_id': account_id,
        'timestamp': str(Path().cwd()),
        'missing_entities': missing_entities,
        'fetched_entities': fetched_entities,
        'recommendations': []
    }
    
    # Add recommendations
    if missing_entities:
        mapping['recommendations'].append({
            'issue': 'Missing entities in alert conditions',
            'solution': 'These entities need to be fetched from New Relic or the alert conditions need to be updated',
            'entities': missing_entities
        })
    
    if fetched_entities:
        mapping['recommendations'].append({
            'issue': 'Successfully fetched entities',
            'solution': 'These entities can be added to your entity files',
            'entities': [e['original_id'] for e in fetched_entities]
        })
    
    # Save mapping file
    mapping_file = f'output/{account_id}_entity_mapping.json'
    try:
        with open(mapping_file, 'w') as f:
            json.dump(mapping, f, indent=2)
        logger.info(f"✓ Entity mapping saved to {mapping_file}")
    except Exception as e:
        logger.error(f"❌ Failed to save entity mapping: {e}")
    
    return mapping_file

def main():
    parser = argparse.ArgumentParser(description='Fix entity resolution issues')
    parser.add_argument('--sourceAccount', required=True, type=int, help='Source account ID')
    parser.add_argument('--sourceRegion', default='us', help='Source region (default: us)')
    parser.add_argument('--sourceApiKey', help='Source API key (or set ENV_SOURCE_API_KEY)')
    
    args = parser.parse_args()
    
    # Get API key
    api_key = args.sourceApiKey or os.environ.get('ENV_SOURCE_API_KEY')
    if not api_key:
        logger.error("❌ No API key provided. Set ENV_SOURCE_API_KEY or use --sourceApiKey")
        return
    
    account_id = args.sourceAccount
    
    # Example entity IDs from your alert rule
    example_entity_ids = [
        'entity-1573815893',
        'entity-1531827693', 
        'entity-1468052827',
        'entity-1573819343',
        'entity-1573816100',
        'entity-1468049122'
    ]
    
    logger.info(f"=== ENTITY RESOLUTION FIX SCRIPT ===")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Region: {args.source_region}")
    logger.info(f"Example entity IDs to check: {example_entity_ids}")
    
    # Step 1: Analyze entity files
    entities, entities_extended = analyze_entity_files(account_id)
    if not entities or not entities_extended:
        logger.error("❌ Cannot proceed without entity files")
        return
    
    # Step 2: Find missing entities
    missing_entities, found_entities = find_missing_entities(example_entity_ids, entities, entities_extended)
    
    # Step 3: Fetch missing entities
    if missing_entities:
        fetched_entities = fetch_missing_entities(missing_entities, api_key, args.source_region)
    else:
        fetched_entities = []
    
    # Step 4: Create mapping file
    mapping_file = create_entity_mapping_file(account_id, missing_entities, fetched_entities)
    
    # Step 5: Summary
    logger.info(f"=== SUMMARY ===")
    logger.info(f"Total entities to check: {len(example_entity_ids)}")
    logger.info(f"Found entities: {len(found_entities)}")
    logger.info(f"Missing entities: {len(missing_entities)}")
    logger.info(f"Successfully fetched: {len(fetched_entities)}")
    
    if missing_entities:
        logger.warning(f"⚠️ Still missing entities: {missing_entities}")
        logger.warning("These entities may not exist in New Relic or may have different IDs")
    
    logger.info(f"✓ Entity mapping saved to: {mapping_file}")
    logger.info("Review the mapping file for detailed information and next steps")

if __name__ == '__main__':
    main()
