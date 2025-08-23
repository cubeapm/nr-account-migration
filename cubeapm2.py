import os
import re
import json
import argparse
import library.localstore as store
import library.migrationlogger as migrationlogger

logger = migrationlogger.get_logger(os.path.basename(__file__))


mapOperator = {
    'ABOVE': '>',
    'ABOVE_OR_EQUALS': '>=',
    'BELOW': '<',
    'BELOW_OR_EQUALS': '<=',
    'EQUALS': '==',
}

# Add mapping for app condition operators
appOperatorMap = {
    'above': '>',
    'above_or_equals': '>=',
    'below': '<',
    'below_or_equals': '<=',
    'equals': '==',
}

idRegEx = r"\(?(?P<idType>appId|appName|entity\.guid)\s*(?:=\s*(?P<guid>\d+|(?:'|\")[^'\"]+(?:'|\"))|IN\s*\((?P<guids>[^)]+)\))\)?"

facetOptionalRegEx = r"(?:FACET\s*(?P<facet>appId|appName|entity\.guid|entity\.name))?"

transactionTypeOptionalRegEx = r"(?:(?:AND\s*)?\(?\s*transactionType\s*=\s*(?:'|\")(?P<transactionType>\w+)(?:'|\")\s*\)?\s*)?"


def resolveEntityGuids(idType, entry, entries_str, entities):
    entries = [entry] if entry else []
    if entries_str:
        entries = entries_str.split(',')
    entries = [x.strip().strip("'\"") for x in entries]
    if not entries:
        raise ValueError("empty entries")
    
    entityType = None
    names = []
    if idType == 'appName':
        entityType = 'APPLICATION'
        names = [{'service': x} for x in entries]
    elif idType == 'appId':
        entityType = 'APPLICATION'
        for e in entries:
            val = int(e)
            # More robust entity filtering that handles missing fields and type mismatches
            _filteredEntities = []
            for x in entities:
                if (x.get('entityType') == 'APM_APPLICATION_ENTITY' and 
                    'applicationId' in x and str(x['applicationId']) == str(val)):
                    _filteredEntities.append(x)
            
            if _filteredEntities:
                names.append({'service': _filteredEntities[0]['name']})
            else:
                # Fallback: use the original app ID as service name
                names.append({'service': f"app-{val}"})
    elif idType == 'entity.guid':
        for guid in entries:
            _filteredEntities = [x for x in entities if x['guid'] == guid]
            if not _filteredEntities:
                raise ValueError("entity not found for guid %s" % guid)
            entity = _filteredEntities[0]
            if not entityType:
                entityType = entity['type']
            elif entity['type'] != entityType:
                raise ValueError("mixed entity type for guid " + guid)
            
            if entityType == 'APPLICATION':
                names.append({'service': entity['name']})
            elif entityType == 'KEY_TRANSACTION':
                names.append({'service': entity['application']['entity']['name'], 'root_name': entity['metricName']})
            else:
                raise ValueError("unhandled entity type " + entityType)
    else:
        raise ValueError("unhandled idType " + idType)
    
    if not names:
        raise ValueError("empty names")
    return entityType, names


def parseFacet(entityType, facet):
    if not facet:
        groupBy = []
    elif facet in ['appId', 'appName']:
        groupBy = ['service']
    elif facet in ['entity.guid', 'entity.name']:
        if entityType == "APPLICATION":
            groupBy = ['service']
        elif entityType == "KEY_TRANSACTION":
            groupBy = ['service', 'root_name']
        else:
            raise ValueError("unhandled entity type " + entityType)
    else:
        raise ValueError("unhandled facet " + facet)
    
    return groupBy


def makeEsr(entityType, names):
    if entityType == "APPLICATION":
        if len(names) == 1:
            fragment = 'service="{}"'.format(dquote(names[0]['service']))
            modelOperator = '='
        else:
            fragment = 'service=~"{}"'.format(requote([x['service'] for x in names]))
            modelOperator = '=~'
        labelPairs = [
            {"label": "service", "operator": modelOperator, "values": [x['service'] for x in names], "options": []},
        ]
    elif entityType == "KEY_TRANSACTION":
        if len(names) == 1:
            fragment = 'service="{}", root_name="{}"'.format(dquote(names[0]['service']), dquote(names[0]['root_name']))
            modelOperator = '='
        else:
            # TODO: this is not entirely correct, as it will do many to many match instead of one to one
            fragment = 'service=~"{}", root_name=~"{}"'.format(requote([x['service'] for x in names]), requote([x['root_name'] for x in names]))
            modelOperator = '=~'
        labelPairs = [
            {"label": "service", "operator": modelOperator, "values": [x['service'] for x in names], "options": []},
            {"label": "root_name", "operator": modelOperator, "values": [x['root_name'] for x in names], "options": []},
        ]
    else:
        raise ValueError("unhandled entity type " + entityType)
    
    return fragment, labelPairs


# TODO properly handle facet in apdex, latency_average, and latency_percentile
def mapQuery(query, all_entities):
    # apdex ############################
    res = re.search(
        r"^\s*SELECT\s+apdex\s*\(apm\.service\.apdex\)\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()
        
        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
            
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        # https://docs.newrelic.com/docs/apm/new-relic-apm/apdex/apdex-measure-user-satisfaction/#apdex-counts
        # Note: We hard-code apdex_t=0.5 here, which is NewRelic's default value.
        # Note: histogram_share(2.0, ...) will include histogram_share(0.5, ...) as well,
        # so we take only half of histogram_share(0.5, ...).
        newQuery = """0.5 *
(
histogram_share(0.5, sum by (service,vmrange) (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
+
histogram_share(2.0, sum by (service,vmrange) (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
)
* sum by (service) (increase(cube_apm_latency_count{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0))
/ sum by (service) (increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0))""".format(fragment=fragment, spanKind=spanKind)

        # print(newQuery)
        return 'APDEX', newQuery, '{}', 1
    
    # request_count ############################
    res = re.search(
        r"^\s*SELECT\s+(?P<rpm1>rate\s*\()?\s*count\s*\(\s*apm\.(?P<mType>service|key)\.transaction\.duration\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if groupdict.get('mType') != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if groupdict.get('mType') != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)
    
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        
        model = {
            "model": {
                "type": "quick",
                "calculate": "rpm",
                "value": "0",
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        if not groupdict.get('rpm1'):
            # The query is counting requests and not requests "per minute"
            model = {}

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            if transactionType == 'Web':
                fragment += ', root_name=~"WebTransaction/.*"'
            elif transactionType == 'Other':
                fragment += ', root_name=~"OtherTransaction/.*"'
            else:
                raise ValueError("unhandled transactionType " + transactionType)

        if groupdict.get('rpm1'):
            newQuery = 'sum(rate(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy} * 60'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        else:
            newQuery = 'sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        return 'REQUEST_COUNT', newQuery, json.dumps(model), 1
    
    # error rate ############################
    res = re.search(
        r"^\s*SELECT\s+(?:\()?\s*count\s*\(apm\.(?P<mType>service|key\.transaction)\.error\.count(\['count'\])?\)\s*/\s*count\s*\(apm\.(?P<mType2>service|key)\.transaction\.duration\)\s*(?:\))?\s*\*\s*100\s+(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if groupdict.get('mType') != 'service' or groupdict.get('mType2') != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if groupdict.get('mType') != 'key.transaction' or groupdict.get('mType2') != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)

        fragment, labelPairs = makeEsr(entityType, names)
        
        spanKind = 'span_kind=~"server|consumer"'
        
        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        
        model = {
            "model": {
                "type": "quick",
                "calculate": "error_percentage",
                "value": "0",
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        newQuery = 'sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}, status_code="ERROR"}} default 0)){groupBy} * 100 / sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        return 'ERROR_PERCENTAGE', newQuery, json.dumps(model), 1
    
    # avg latency ############################
    res = re.search(
        r"^\s*SELECT\s+average\s*\(\s*apm\.(?P<mType>service|key)\.(?P<mType2>transaction|datastore)\.duration\s*\)\s*(?P<thousand>\*\s*1000)?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
        r"^\s*SELECT\s+average\s*\(\s*convert\s*\(\s*apm\.(?P<mType>service|key)\.(?P<mType2>transaction|datastore)\.duration\s*,\s*unit\s*,\s*(?:'|\")(?P<thousand>ms)(?:'|\")\s*\)\s*\)\s*\)\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
        r"^\s*FROM\s+(?P<lambdaMarker>AwsLambdaInvocation)\s+SELECT\s+average\s*\(\s*duration\s*\)\s*WHERE\s+aws\.lambda\.arn\s*=\s*['\"]arn:aws:lambda:[\w-]+:\d+:function:(?P<guid>[^'\"]+)['\"]\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()
        mType = 'service' if groupdict.get('lambdaMarker') else groupdict.get('mType')

        thresholdMultiplier = 1 if groupdict.get('thousand') else 1000

        try:
            entityType, names = resolveEntityGuids('appName' if groupdict.get('lambdaMarker') else groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if mType != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if mType != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)
            
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, 'appName' if groupdict.get('lambdaMarker') else groupdict.get('facet'))

        model = {
            "model": {
                "type": "quick",
                "calculate": "latency_average",
                "value": "0",
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        mType2 = 'transaction' if groupdict.get('lambdaMarker') else groupdict.get('mType2')
        if mType2 == 'datastore':
            model = {}
            fragment += ', span_name=~"Datastore/.*"'
            spanKind = 'span_kind="client"'
        elif mType2 != 'transaction':
            raise ValueError("unhandled mType2 " + mType2)

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            if transactionType == 'Web':
                fragment += ', root_name=~"WebTransaction/.*"'
            elif transactionType == 'Other':
                fragment += ', root_name=~"OtherTransaction/.*"'
            else:
                raise ValueError("unhandled transactionType " + transactionType)
        
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        newQuery = 'sum(increase(cube_apm_latency_sum{{{fragment}, {spanKind}}} default 0)){groupBy} * 1000 / sum(increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        return 'LATENCY_AVERAGE', newQuery, json.dumps(model), thresholdMultiplier
    
    # percentile latency ############################
    res = re.search(
        r"^\s*SELECT\s+percentile\s*\(\s*duration\s*,\s*(?P<percentile>[0-9\.]+)\s*\)\s+FROM\s+Transaction\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        thresholdMultiplier = 1000

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
            
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        percentile = groupdict['percentile']

        model = {
            "model": {
                "type": "quick",
                "calculate": "latency_percentile",
                "value": percentile,
                "labelPairs": labelPairs,
                "groupBy": ['service'],
            },
        }

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            if transactionType == 'Web':
                fragment += ', root_name=~"WebTransaction/.*"'
            elif transactionType == 'Other':
                fragment += ', root_name=~"OtherTransaction/.*"'
            else:
                raise ValueError("unhandled transactionType " + transactionType)
        
        newQuery = 'histogram_quantile({percentile}/100, sum(increase(cube_apm_latency_bucket{{{fragment}, {spanKind}}} default 0)) by (vmrange, service)) * 1000'.format(fragment=fragment, spanKind=spanKind, percentile=percentile)
        
        # print(newQuery)
        return 'LATENCY_PERCENTILE', newQuery, json.dumps(model), thresholdMultiplier

    # AWS metrics ############################
    # res = re.search(
    #     r"^\s*SELECT\s+average\s*\(\s*`aws\.(?P<awsService>\w+)\.(?P<awsMetric>\w+)`\s*\)\s*FROM\s+Metric\s+FACET\s*`aws\.(?P=awsService)\.(?P<awsDimension>)`\s*WHERE\s+`collector.name`\s*=\s*['\"][\w-]+['\"]\s*AND\s*`aws\.accountId`\s*=\s*['\"]' AND `aws.Arn` LIKE 'arn:aws:rds:ap-south-1:411342004333:db:checkout-prod-readonly'\s*$",
    #     query, flags=re.IGNORECASE
    # )
    # if res:
    #     groupdict = res.groupdict()

    return "UNHANDLED", query, '{}', 1


def mapAppCondition(app_condition, all_entities):
    """
    Map app conditions (like apm_app_metric) to Prometheus queries
    """
    condition_type = app_condition['type']
    metric = app_condition.get('metric', None)  # Some conditions don't have metric field
    entities = app_condition.get('entities', [])  # Some conditions don't have entities field
    
    # If no entities specified, this is a policy-level condition
    if not entities:
        entities = ["policy-level"]
    
    if condition_type == 'apm_app_metric' and metric == 'error_percentage':
        # Handle error percentage for APM applications
        try:
            # Get entity information
            entity_guids = entities
            names = []
            for guid in entity_guids:
                if guid == "policy-level":
                    # Policy-level condition - use dummy service
                    names.append({'service': 'dummy'})
                else:
                    try:
                        # More robust entity filtering that handles missing fields and type mismatches
                        filtered_entities = []
                        for x in all_entities:
                            # Check if entity has the required fields before accessing them
                            if 'guid' in x and str(x['guid']) == str(guid):
                                filtered_entities.append(x)
                            elif 'id' in x and str(x['id']) == str(guid):
                                filtered_entities.append(x)
                            # Also try with the clean entity ID (without 'entity-' prefix)
                            elif guid.startswith('entity-'):
                                clean_guid = guid.replace('entity-', '')
                                if 'guid' in x and str(x['guid']) == str(clean_guid):
                                    filtered_entities.append(x)
                                elif 'id' in x and str(x['id']) == str(clean_guid):
                                    filtered_entities.append(x)
                    except Exception as e:
                        # If there's an error accessing fields, use the entity ID as fallback
                        logger.warning(f"Error filtering entities for guid {guid}: {e}")
                        filtered_entities = []
                    
                    if filtered_entities:
                        entity = filtered_entities[0]
                        names.append({'service': entity['name']})
                    
            if not names:
                # Fallback to using the original entity IDs
                names = [{'service': f"entity-{guid}"} for guid in entity_guids]
            
            # Create service filter
            if len(names) == 1:
                fragment = 'service="{}"'.format(dquote(names[0]['service']))
            else:
                fragment = 'service=~"{}"'.format(requote([x['service'] for x in names]))
            
            # Create the error percentage query
            spanKind = 'span_kind=~"server|consumer"'
            newQuery = 'sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}, status_code="ERROR"}} default 0)) by (service) * 100 / sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)) by (service)'.format(
                fragment=fragment, spanKind=spanKind
            )
            
            # Create model configuration
            model = {
                "model": {
                    "type": "quick",
                    "calculate": "error_percentage",
                    "value": "0",
                    "labelPairs": [
                        {"label": "service", "operator": "=" if len(names) == 1 else "=~", "values": [x['service'] for x in names], "options": []}
                    ],
                    "groupBy": ["service"],
                },
            }
            
            return 'ERROR_PERCENTAGE', newQuery, json.dumps(model), 1
            
        except Exception as ex:
            return "ERROR", f"Error processing app condition: {ex}", '{}', 1
    
    elif condition_type == 'apm_response_time_percentile':
        # Handle response time percentile for APM applications
        try:
            # Get entity information
            entity_guids = entities
            names = []
            
            if entity_guids:
                # Entity-specific condition
                for guid in entity_guids:
                    if guid == "policy-level":
                        # Policy-level condition - use dummy service
                        names.append({'service': 'dummy'})
                    else:
                        # More robust entity filtering that handles missing fields and type mismatches
                        filtered_entities = []
                        for x in all_entities:
                            # Check if entity has the required fields before accessing them
                            if 'guid' in x and str(x['guid']) == str(guid):
                                filtered_entities.append(x)
                            elif 'applicationId' in x and str(x['applicationId']) == str(guid):
                                filtered_entities.append(x)
                            # Also try with the clean entity ID (without 'entity-' prefix)
                            elif guid.startswith('entity-'):
                                clean_guid = guid.replace('entity-', '')
                                if 'guid' in x and str(x['guid']) == str(clean_guid):
                                    filtered_entities.append(x)
                                elif 'applicationId' in x and str(x['applicationId']) == str(clean_guid):
                                    filtered_entities.append(x)
                        if filtered_entities:
                            entity = filtered_entities[0]
                            if entity['entityType'] == 'APM_APPLICATION_ENTITY':
                                names.append({'service': entity['name']})
                            else:
                                # Use the original entity ID if type is not supported
                                names.append({'service': f"entity-{guid}"})
                        else:
                            # Use the original entity ID if not found in entities file
                            names.append({'service': f"entity-{guid}"})
                
                if not names:
                    # Fallback to using the original entity IDs
                    names = [{'service': f"entity-{guid}"} for guid in entity_guids]
                
                # Create service filter
                if len(names) == 1:
                    fragment = 'service="{}"'.format(dquote(names[0]['service']))
                else:
                    fragment = 'service=~"{}"'.format(requote([x['service'] for x in names]))
                
                # Create model configuration
                model = {
                    "model": {
                        "type": "quick",
                        "calculate": "latency_percentile",
                        "value": app_condition.get('percentile_value', '90'),
                        "labelPairs": [
                            {"label": "service", "operator": "=" if len(names) == 1 else "=~", "values": [x['service'] for x in names], "options": []}
                        ],
                        "groupBy": ["service"],
                    },
                }
            else:
                # Policy-level condition (no specific entities) - use "dummy" entity
                fragment = 'service="dummy"'
                model = {
                    "model": {
                        "type": "quick",
                        "calculate": "latency_percentile",
                        "value": app_condition.get('percentile_value', '90'),
                        "labelPairs": [
                            {"label": "service", "operator": "=", "values": ["dummy"], "options": []}
                        ],
                        "groupBy": ["service"],
                    },
                }
            
            # Get percentile value (default to 90 if not specified)
            percentile_value = app_condition.get('percentile_value', '90')
            percentile_decimal = float(percentile_value) / 100.0
            
            # Create the response time percentile query
            spanKind = 'span_kind=~"server|consumer"'
            newQuery = 'histogram_quantile({percentile}, sum(increase(cube_apm_latency_bucket{{{fragment}, {spanKind}}} default 0)) by (vmrange, service)) * 1000'.format(
                fragment=fragment, spanKind=spanKind, percentile=percentile_decimal
            )
            
            return 'LATENCY_PERCENTILE', newQuery, json.dumps(model), 1000
            
        except Exception as ex:
            return "ERROR", f"Error processing app condition: {ex}", '{}', 1
    
    if metric:
        return "UNHANDLED", f"Unhandled app condition type: {condition_type}, metric: {metric}", '{}', 1
    else:
        return "UNHANDLED", f"Unhandled app condition type: {condition_type}", '{}', 1


def migrate(src_acct_id, mode):
    policies_list = store.load_json_file(src_acct_id, store.ALERT_POLICIES_DIR, 'alert_policies.json')
    all_policies = { policy['id'] : policy for policy in policies_list['policies'] }
    all_entities = store.load_json_from_file('output', '%s_entities_extended.json' % str(src_acct_id))
    all_alert_conditions = store.load_json_file(src_acct_id, store.ALERT_POLICIES_DIR, 'alert_conditions.json')

    # Process NRQL conditions
    for condition in all_alert_conditions['nrql']:
        conditionType = condition['type']
        if conditionType not in ['STATIC', 'BASELINE']:
            raise ValueError("unhandled conditionType " + conditionType)
        baselineDirection = None
        if conditionType == 'BASELINE':
            baselineDirection = condition['baselineDirection']
            if baselineDirection not in ['UPPER_ONLY', 'LOWER_ONLY', 'UPPER_AND_LOWER']:
                raise ValueError("unhandled baselineDirection " + baselineDirection)

        datasource = 'prometheus'
        kind = 'anomaly' if conditionType == 'BASELINE' else 'static'
        name = condition['name']
        interval = condition['signal']['aggregationWindow']
        # expr = condition['nrql']['query']
        expr2 = ''
        # forValue = condition['terms']['thresholdDuration']
        labels = json.dumps({"group": all_policies[int(condition['policyId'])]['name']})
        annotations = '{}'
        status = 'ACTIVE' if condition['enabled'] else 'PAUSED'
        config = '{}'
        receiver = json.dumps({
            "email_configs":[],
            "slack_configs":[],
            "pagerduty_configs":[],
            "googlechat_configs":[],
            "webhook_configs":[
                {
                    "url":"http://localhost",
                    "type":"webhook",
                    "send_resolved":True,
                    "cube_show_query":False,
                    "valid":True
                }
        ]})
        repeat_interval = 14400

        query = condition['nrql']['query']
        qType, query, config, thresholdMultiplier = mapQuery(query, all_entities)
        if qType in ["UNHANDLED", "ERROR"]:
            name = "[{}] {}".format(qType, name)

        terms = condition['terms']
        if len(terms) == 1:
            forValue = terms[0]['thresholdDuration']
            threshold = terms[0]['threshold']
            operator = mapOperator[terms[0]['operator']]
            if conditionType == 'STATIC':
                threshold = threshold * thresholdMultiplier
            if conditionType == 'BASELINE':
                if baselineDirection == 'UPPER_ONLY':
                    operator = '>'
                elif baselineDirection == 'LOWER_ONLY':
                    operator = '<'
                elif baselineDirection == 'UPPER_AND_LOWER':
                    operator = '<>'

            expr = "({query}) {operator} {threshold}".format(query=query, operator=operator, threshold=threshold)
        elif len(terms) == 2:
            if terms[0]['priority'] == 'WARNING':
                warningTerms = terms[0]
            elif terms[0]['priority'] == 'CRITICAL':
                criticalTerms = terms[0]
            else:
                raise ValueError("unhandled terms.priority for condition id " + condition["id"])
            if terms[1]['priority'] == 'WARNING':
                warningTerms = terms[1]
            elif terms[1]['priority'] == 'CRITICAL':
                criticalTerms = terms[1]
            else:
                raise ValueError("unhandled terms.priority for condition id " + condition["id"])
            
            if not warningTerms or not criticalTerms:
                raise ValueError("unhandled terms for condition id " + condition["id"])
            
            forValue = warningTerms['thresholdDuration']
            wThreshold = warningTerms['threshold']
            wOperator = mapOperator[warningTerms['operator']]
            cThreshold = criticalTerms['threshold']
            cOperator = mapOperator[criticalTerms['operator']]
            if conditionType == 'STATIC':
                wThreshold = wThreshold * thresholdMultiplier
                cThreshold = cThreshold * thresholdMultiplier
            if conditionType == 'BASELINE':
                if baselineDirection == 'UPPER_ONLY':
                    wOperator = '>'
                    cOperator = '>'
                elif baselineDirection == 'LOWER_ONLY':
                    wOperator = '<'
                    cOperator = '<'
                elif baselineDirection == 'UPPER_AND_LOWER':
                    wOperator = '<>'
                    cOperator = '<>'

            expr = "({query}) {operator} {threshold}".format(query=query, operator=wOperator, threshold=wThreshold)
            expr2 = "({query}) {operator} {threshold}".format(query=query, operator=cOperator, threshold=cThreshold)
        else:
            raise ValueError("unhandled len(terms) for condition id " + condition["id"])

        if mode == 'mysql':
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, `interval`, expr, expr2, `for`, labels, annotations, status, config, receiver, repeat_interval, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval)
        else:
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, "interval", expr, expr2, "for", labels, annotations, status, config, receiver, repeat_interval, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval)
        
        print(statement)

    for condition in all_alert_conditions['app']:
        conditionType = 'STATIC'  # App conditions are always static
        baselineDirection = None
        
        datasource = 'prometheus'
        kind = 'static'
        name = condition['name']
        interval = 60  # Default interval for app conditions
        expr2 = ''
        # Try to get policy name, but handle cases where policyId is missing
        policy_name = 'Unknown Policy'
        if 'policyId' in condition and condition['policyId'] in all_policies:
            policy_name = all_policies[int(condition['policyId'])]['name']
        labels = json.dumps({"group": policy_name})
        annotations = '{}'
        status = 'ACTIVE' if condition['enabled'] else 'PAUSED'
        config = '{}'
        receiver = json.dumps({
            "email_configs":[],
            "slack_configs":[],
            "pagerduty_configs":[],
            "googlechat_configs":[],
            "webhook_configs":[
                {
                    "url":"http://localhost",
                    "type":"webhook",
                    "send_resolved":True,
                    "cube_show_query":False,
                    "valid":True
                }
        ]})
        repeat_interval = 14400

        # Map app condition to query
        qType, query, config, thresholdMultiplier = mapAppCondition(condition, all_entities)
        if qType in ["UNHANDLED", "ERROR"]:
            name = "[{}] {}".format(qType, name)

        terms = condition['terms']
        if len(terms) == 1:
            forValue = int(terms[0]['duration']) * 60  # Convert minutes to seconds
            threshold = terms[0]['threshold']
            operator = appOperatorMap[terms[0]['operator']]
            # Don't multiply threshold by thresholdMultiplier - it's meant for query generation, not threshold calculation

            expr = "({query}) {operator} {threshold}".format(query=query, operator=operator, threshold=threshold)
        elif len(terms) == 2:
            if terms[0]['priority'] == 'warning':
                warningTerms = terms[0]
            elif terms[0]['priority'] == 'critical':
                criticalTerms = terms[0]
            else:
                raise ValueError("unhandled terms.priority for condition id " + str(condition["id"]))
            if terms[1]['priority'] == 'warning':
                warningTerms = terms[1]
            elif terms[1]['priority'] == 'critical':
                criticalTerms = terms[1]
            else:
                raise ValueError("unhandled terms.priority for condition id " + str(condition["id"]))
            
            if not warningTerms or not criticalTerms:
                raise ValueError("unhandled terms for condition id " + str(condition["id"]))
            
            forValue = int(warningTerms['duration']) * 60  # Convert minutes to seconds
            wThreshold = warningTerms['threshold']
            wOperator = appOperatorMap[warningTerms['operator']]
            cThreshold = criticalTerms['threshold']
            cOperator = appOperatorMap[criticalTerms['operator']]
            # Don't multiply thresholds by thresholdMultiplier - it's meant for query generation, not threshold calculation

            expr = "({query}) {operator} {threshold}".format(query=query, operator=wOperator, threshold=wThreshold)
            expr2 = "({query}) {operator} {threshold}".format(query=query, operator=cOperator, threshold=cThreshold)
        else:
            raise ValueError("unhandled len(terms) for condition id " + str(condition["id"]))

        if mode == 'mysql':
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, `interval`, expr, expr2, `for`, labels, annotations, status, config, receiver, repeat_interval, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval)
        else:
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, "interval", expr, expr2, "for", labels, annotations, status, config, receiver, repeat_interval, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval)
        
        print(statement)


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
        '--mode',
        '--mode',
        nargs=1,
        type=str,
        required=True,
        help='mysql or postgresql',
        dest='mode'
    )
    return parser


def main():
    # parser = create_argument_parser()
    # args = parser.parse_args()
    
    migrate(1642117, "postgresql")


def dquote(str):
    return str.replace('\\', '\\\\').replace('"', '\\"')

def squoteSQL(str, mode):
    if mode == 'mysql':
        return str.replace('\\', '\\\\').replace("'", "\\'")
    elif mode == 'postgresql':
        return str.replace('\\', '\\\\').replace("'", "''")
    raise ValueError("invalid mode")

def requote(str_list):
    return '|'.join([re.escape(x) for x in str_list])


if __name__ == '__main__':
    main()
