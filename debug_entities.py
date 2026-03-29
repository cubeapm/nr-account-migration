#!/usr/bin/env python3
"""
Simple Entity Debug Script

This script analyzes your entity files to understand why entities aren't being resolved.
"""

import json
import os
from pathlib import Path

def analyze_entity_files(account_id):
    """Analyze entity files to understand the current state"""
    print(f"=== ANALYZING ENTITY FILES FOR ACCOUNT {account_id} ===")
    
    # Check if entity files exist
    entities_file = f'output/{account_id}_entities.json'
    entities_extended_file = f'output/{account_id}_entities_extended.json'
    
    if not os.path.exists(entities_file):
        print(f"❌ Entity file not found: {entities_file}")
        return
    
    if not os.path.exists(entities_extended_file):
        print(f"❌ Extended entity file not found: {entities_extended_file}")
        return
    
    # Load entity files
    try:
        with open(entities_file, 'r') as f:
            entities = json.load(f)
        print(f"✓ Loaded {len(entities)} base entities")
        
        with open(entities_extended_file, 'r') as f:
            entities_extended = json.load(f)
        print(f"✓ Loaded {len(entities_extended)} extended entities")
    except Exception as e:
        print(f"❌ Failed to load entity files: {e}")
        return
    
    # Analyze entity types
    entity_types = {}
    for entity in entities_extended:
        entity_type = entity.get('entityType', 'UNKNOWN')
        entity_types[entity_type] = entity_types.get(entity_type, 0) + 1
    
    print(f"\nEntity types found:")
    for entity_type, count in entity_types.items():
        print(f"  {entity_type}: {count}")
    
    # Check for entities with applicationId
    entities_with_app_id = [e for e in entities_extended if 'applicationId' in e]
    print(f"\nEntities with applicationId: {len(entities_with_app_id)}")
    
    # Check for entities with guid
    entities_with_guid = [e for e in entities_extended if 'guid' in e]
    print(f"Entities with guid: {len(entities_with_guid)}")
    
    # Show sample entities
    print(f"\nSample entities:")
    for i, entity in enumerate(entities_extended[:5]):
        print(f"  #{i+1}:")
        print(f"    entityType: {entity.get('entityType', 'N/A')}")
        print(f"    applicationId: {entity.get('applicationId', 'N/A')}")
        print(f"    guid: {entity.get('guid', 'N/A')}")
        print(f"    name: {entity.get('name', 'N/A')}")
        print()
    
    # Check for the specific entity IDs from your alert rule
    required_entity_ids = [
        'entity-1573815893',
        'entity-1531827693', 
        'entity-1468052827',
        'entity-1573819343',
        'entity-1573816100',
        'entity-1468049122'
    ]
    
    print(f"=== CHECKING REQUIRED ENTITY IDS ===")
    found_entities = []
    missing_entities = []
    
    for entity_id in required_entity_ids:
        clean_id = entity_id.replace('entity-', '')
        print(f"\nChecking entity: {entity_id} (clean ID: {clean_id})")
        
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
            print(f"  ✓ Entity {entity_id} found")
            found_entities.append(entity_id)
        else:
            print(f"  ❌ Entity {entity_id} NOT FOUND")
            missing_entities.append(entity_id)
    
    print(f"\n=== SUMMARY ===")
    print(f"Total entities to check: {len(required_entity_ids)}")
    print(f"Found entities: {len(found_entities)}")
    print(f"Missing entities: {len(missing_entities)}")
    
    if missing_entities:
        print(f"\n❌ Missing entities: {missing_entities}")
        print("These entities are referenced in your alert conditions but don't exist in your entity files.")
        print("\nPossible causes:")
        print("1. The entities were never fetched from New Relic")
        print("2. The entity IDs in alert conditions are incorrect")
        print("3. The entities exist but with different IDs")
        print("4. The entities were deleted from New Relic")
        
        print(f"\nRecommendations:")
        print("1. Run your entity fetching script to get the latest entities")
        print("2. Check if the entity IDs in alert conditions are correct")
        print("3. Verify that these entities still exist in New Relic")
    else:
        print(f"\n✓ All entities found! The issue might be elsewhere in your code.")

def main():
    # Try to find account ID from output directory
    output_dir = Path('output')
    if output_dir.exists():
        entity_files = list(output_dir.glob('*_entities.json'))
        if entity_files:
            # Extract account ID from first file
            account_id = entity_files[0].stem.replace('_entities', '')
            print(f"Found entity files for account: {account_id}")
            analyze_entity_files(account_id)
        else:
            print("No entity files found in output directory")
    else:
        print("Output directory not found")

if __name__ == '__main__':
    main()
