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

    'above': '>',
    'above_or_equals': '>=',
    'below': '<',
    'below_or_equals': '<=',
    'equals': '==',
}

idRegEx = r"\(?\`?(?P<idType>appId|appName|entity\.guid|entityGuid)\`?\s*(?:=\s*(?P<guid>\d+|''[^']+''|(?:'|\")[^'\"]+(?:'|\"))|IN\s*\((?P<guids>[^)]+)\))\)?"

facetOptionalRegEx = r"(?:FACET\s*\`?(?P<facet>appId|appName|entity\.guid|entity\.name|transactionName)\`?)?"

# Require AND/and before transactionType so we never zero-match while "and transactionType=..." remains (see appId ... and transactionType ...).
transactionTypeOptionalRegEx = r"(?:\s*(?:AND|and)\s+\(?\s*transactionType\s*=\s*(?P<txn_q>''|['\"])(?P<transactionType>\w+)(?P=txn_q)\s*\)?\s*)?"
transactionNameOptionalRegEx = r"(?:(?:AND\s*)?\(?\s*transactionName\s*=\s*(?P<transactionName>''[^']+''|'[^']+'|\"[^\"]+\")\s*\)?\s*)?"


def resolveEntityGuids(idType, entry, entries_str, entities):
    entries = [entry] if entry else []
    if entries_str:
        entries = entries_str.split(',')
    entries = [x.strip().strip("'\"") for x in entries]
    if not entries:
        raise ValueError("empty entries")
    
    entityType = None
    names = []
    isUnResolvedGUID = False
    if idType == 'appName':
        entityType = 'APPLICATION'
        names = [{'service': x} for x in entries]
    elif idType == 'appId':
        entityType = 'APPLICATION'
        for e in entries:
            val = int(e)
            _filteredEntities = [x for x in entities if x['entityType'] == 'APM_APPLICATION_ENTITY' and x['applicationId'] == val]
            if not _filteredEntities:
                # raise ValueError("entity not found for appId %s" % e)
                # Keep appId instead of raising error, so that we can translate the query
                entityType = 'APPLICATION'
                isUnResolvedGUID = True
                names.append({'service': e})
                continue
            names.append({'service': _filteredEntities[0]['name']})
    elif idType == 'entity.guid' or idType == 'entityGuid':
        for guid in entries:
            _filteredEntities = [x for x in entities if x['guid'] == guid]
            if not _filteredEntities:
                # raise ValueError("entity not found for guid %s" % guid)
                # Keep guid instead of raising error, so that we can translate the query
                # Note: our assumption that entityType = 'APPLICATION' isn't safe
                entityType = 'APPLICATION'
                isUnResolvedGUID = True
                names.append({'service': guid})
                continue

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
    return entityType, names, isUnResolvedGUID


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
    elif facet == 'transactionName':
        groupBy = ['root_name']
    else:
        raise ValueError("unhandled facet " + facet)
    
    return groupBy


def strip_nr_quoted_literal(value):
    """Strip NR/SQL string wrappers: 'x', \"x\", or ''x'' (escaped singles in SQL dumps)."""
    if value is None:
        return None
    s = value.strip()
    if len(s) >= 4 and s.startswith("''") and s.endswith("''"):
        return s[2:-2].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def makeEsr(entityType, names, serviceLabel='service'):
    if entityType == "APPLICATION":
        if len(names) == 1:
            fragment = '{}="{}"'.format(serviceLabel, dquote(names[0]['service']))
            modelOperator = '='
        else:
            fragment = '{}=~"{}"'.format(serviceLabel, requote([x['service'] for x in names]))
            modelOperator = '=~'
        labelPairs = [
            {"label": serviceLabel, "operator": modelOperator, "values": [x['service'] for x in names], "options": []},
        ]
    elif entityType == "KEY_TRANSACTION":
        if len(names) == 1:
            fragment = '{}="{}", root_name="{}"'.format(serviceLabel, dquote(names[0]['service']), dquote(names[0]['root_name']))
            modelOperator = '='
        else:
            # TODO: this is not entirely correct, as it will do many to many match instead of one to one
            fragment = '{}=~"{}", root_name=~"{}"'.format(serviceLabel, requote([x['service'] for x in names]), requote([x['root_name'] for x in names]))
            modelOperator = '=~'
        labelPairs = [
            {"label": serviceLabel, "operator": modelOperator, "values": [x['service'] for x in names], "options": []},
            {"label": "root_name", "operator": modelOperator, "values": [x['root_name'] for x in names], "options": []},
        ]
    else:
        raise ValueError("unhandled entity type " + entityType)
    
    return fragment, labelPairs


def mapQuery(query, all_entities, compat):
    # apdex ############################
    res = re.search(
        r"^\s*SELECT\s+apdex\s*\(\`?apm\.service\.apdex\`?\)\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
         r"^\s*SELECT\s+apdex\s*\(\s*newrelic\.timeslice\.value\s*\)\s*"
        r"(?:AS\s*(?:\w+|''[^']*''|'[^']*'|\"[^\"]*\"))?\s*"
        r"FROM\s+Metric\s+WHERE\s+"
        r"(?:metricTimesliceName\s*=\s*(?:''[^']*''|'[^']*'|\"[^\"]*\")\s*AND\s+)?"
        + idRegEx + r"\s*"
        + facetOptionalRegEx + r"\s*$",
        query,
        flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()
        
        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
            
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        groupByWithVmrange = groupBy + ['vmrange']
        groupByWithVmrangeStr = ' by ({})'.format(','.join(groupByWithVmrange))

        # https://docs.newrelic.com/docs/apm/new-relic-apm/apdex/apdex-measure-user-satisfaction/#apdex-counts
        # Note: We hard-code apdex_t=0.5 here, which is NewRelic's default value.
        # Note: histogram_share(2.0, ...) will include histogram_share(0.5, ...) as well,
        # so we take only half of histogram_share(0.5, ...).
        newQuery = """0.5 *
(
histogram_share(0.5, sum{groupByWithVmrange} (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
+
histogram_share(2.0, sum{groupByWithVmrange} (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
)
* sum{groupBy} (increase(cube_apm_latency_count{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0))
/ sum{groupBy} (increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0))""".format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr, groupByWithVmrange=groupByWithVmrangeStr)

        # print(newQuery)
        if isUnResolvedGUID:
            return 'GUID', newQuery, '{}', 1
        return 'APDEX', newQuery, '{}', 1
    
    # request_count — Transaction: rate(count(duration), 1 minute) WHERE entityGuid + transactionType + name ############################
    res = re.search(
        r"^\s*\(?\s*SELECT\s+"
        r"(?P<rpm1>rate\s*\()?\s*count\s*\(\s*duration\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*"
        r"(?:AS\s+(?:\w+|''[^']*''|'[^']+'|\"[^\"]+\"))?\s*"
        r"FROM\s+Transaction\s+WHERE\s+"
        r"(?:\(\s*)?"
        + idRegEx
        + r"\s*(?:\)\s*)?"
        r"\s*(?:AND|and)\s+"
        r"(?:\(\s*)?`?transactionType`?\s*=\s*(?P<txn_q>''|['\"])(?P<transactionType>\w+)(?P=txn_q)\s*(?:\)\s*)?"
        r"\s*(?:AND|and)\s+"
        r"(?:\(\s*)?`?name`?\s*=\s*(?P<txnNameVal>''[^']+''|'[^']+'|\"[^\"]+\")\s*(?:\)\s*)?"
        r"\s*(?:EXTRAPOLATE)?\s*\)?\s*$",
        query,
        flags=re.IGNORECASE,
    )
    if res:
        groupdict = res.groupdict()
        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(
                groupdict.get("idType"), groupdict.get("guid"), groupdict.get("guids"), all_entities
            )
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1
        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
        fragment, labelPairs = makeEsr(entityType, names)
        spanKind = 'span_kind=~"server|consumer"'
        groupBy = parseFacet(entityType, groupdict.get("facet"))
        groupByStr = " by ({})".format(",".join(groupBy)) if groupBy else ""
        transaction_type = strip_nr_quoted_literal(groupdict.get("transactionType") or "")
        txn_name = strip_nr_quoted_literal(groupdict["txnNameVal"])
        if transaction_type and transaction_type not in ("Web", "Other"):
            raise ValueError("unhandled transactionType " + transaction_type)
        model = {}
        fragment += ', root_name="{}"'.format(dquote(txn_name))
        if groupdict.get("rpm1"):
            newQuery = "sum(rate(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy} * 60".format(
                fragment=fragment, spanKind=spanKind, groupBy=groupByStr
            )
        else:
            newQuery = "sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy}".format(
                fragment=fragment, spanKind=spanKind, groupBy=groupByStr
            )
        if isUnResolvedGUID:
            return "GUID", newQuery, json.dumps(model), 1
        return "REQUEST_COUNT", newQuery, json.dumps(model), 1

    # request_count ############################
    res = re.search(
        r"^\s*\(?\s*SELECT\s+(?P<rpm1>rate\s*\()?\s*count\s*\(\s*apm\.(?P<mType>service|key)\.transaction\.duration\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + transactionNameOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*\)?\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
        r"^\s*\(?\s*SELECT\s+filter\s*\(\s*count\s*\(\s*newrelic\.timeslice\.value\s*\)\s*,\s*WHERE\s+metricTimesliceName\s*=\s*(?:''[^']*''|'[^']*'|\"[^\"]*\")\s*\)\s*OR\s*0\s*"
        r"FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*"
        r"(?:AND\s+metricTimesliceName\s+IN\s*\([^)]+\)\s*)?"
        + facetOptionalRegEx + r"\s*\)?\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        mType = groupdict.get('mType')
        if entityType == "APPLICATION":
            if mType and mType != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if mType and mType != 'key':
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
            transactionType = transactionType.strip("'\"")
            model = {}
            if transactionType == 'Web':
                fragment += ', root_name=~"WebTransaction/.*"'
            elif transactionType == 'Other':
                fragment += ', root_name=~"OtherTransaction/.*"'
            else:
                raise ValueError("unhandled transactionType " + transactionType)

        transactionName = groupdict.get('transactionName')
        if transactionName:
            model = {}
            transactionName = transactionName.strip("'\"")
            fragment += ', root_name="{}"'.format(dquote(transactionName))

        if groupdict.get('rpm1'):
            newQuery = 'sum(rate(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy} * 60'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        else:
            newQuery = 'sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        if isUnResolvedGUID:
            return 'GUID', newQuery, json.dumps(model), 1
        return 'REQUEST_COUNT', newQuery, json.dumps(model), 1
    
    # error rate ############################
    res = re.search(
        r"^\s*SELECT\s+(?:\()?\s*count\s*\(apm\.(?P<mType>service|key\.transaction)\.error\.count\)\s*(?P<hundred1>\*\s*100)?\s*/\s*count\s*\(apm\.(?P<mType2>service|key)\.transaction\.duration\)\s*(?:\))?\s*(?P<hundred2>\*\s*100)?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
        r"^\s*SELECT\s+(?:\()?\s*sum\s*\(apm\.(?P<mType>service|key\.transaction)\.error\.count(\['count'\])?\)\s*(?P<hundred1>\*\s*100)?\s*/\s*count\s*\(apm\.(?P<mType2>service|key)\.transaction\.duration\)\s*(?:\))?\s*(?P<hundred2>\*\s*100)?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
        r"^\s*\(?\s*SELECT\s+\(\(\s*filter\s*\(\s*count\s*\(\s*newrelic\.timeslice\.value\s*\)\s*,\s*where\s+metricTimesliceName\s*=\s*(?:''[^']*''|'[^']*'|\"[^\"]*\")\s*\)\s*"
        r"/\s*filter\s*\(\s*count\s*\(\s*newrelic\.timeslice\.value\s*\)\s*,\s*where\s+metricTimesliceName\s+IN\s*\([^)]+\)\s*\)\s*\)\s*OR\s*0\s*\)\s*\*\s*(?P<hundred1>100)\s*"
        r"FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*"
        r"(?:AND\s+metricTimesliceName\s+IN\s*\([^)]+\)\s*)?"
        + facetOptionalRegEx + r"\s*\)?\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
    r"^\s*\(?\s*SELECT\s+sum\s*\(\s*apm\.(?P<mType>service)\.transaction\.error\.count\s*\[\s*(?:''count''|'count'|\"count\")\s*\]\s*\)\s*"
    r"/\s*count\s*\(\s*apm\.(?P<mType2>service)\.transaction\.duration\s*\)\s*"
    r"(?:AS\s*(?:\w+|''[^']*''|'[^']*'|\"[^\"]*\"))?\s*"
    r"FROM\s+Metric\s+WHERE\s*"
    r"\(?\s*`?(?P<idType>entity\.guid|entityGuid)\`?\s*=\s*(?P<guid>''[^']+''|'[^']+'|\"[^\"]+\")\s*\)?\s*"
    r"AND\s*\(\(\s*transactionType\s*=\s*(?P<transactionType>''Web''|''Other''|'Web'|'Other'|\"Web\"|\"Other\")\s*"
    r"AND\s*transactionName\s*=\s*(?P<transactionName>''[^']+''|'[^']+'|\"[^\"]+\")\s*\)\)\s*\)?\s*$",
    query,
    flags=re.IGNORECASE
)
    if res:
        groupdict = res.groupdict()

        thresholdMultiplier = 1 if groupdict.get('hundred1') or groupdict.get('hundred2') else 100

        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        mType = groupdict.get('mType')
        mType2 = groupdict.get('mType2')
        if entityType == "APPLICATION":
            if (mType and mType != 'service') or (mType2 and mType2 != 'service'):
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if (mType and mType != 'key.transaction') or (mType2 and mType2 != 'key'):
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

        transactionType = groupdict.get('transactionType')
        if transactionType:
            transactionType = transactionType.strip("'\"")
            model = {}
            if transactionType == 'Web':
                fragment += ', root_name=~"WebTransaction/.*"'
            elif transactionType == 'Other':
                fragment += ', root_name=~"OtherTransaction/.*"'
            else:
                raise ValueError("unhandled transactionType " + transactionType)

        newQuery = 'sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}, status_code="ERROR"}} default 0)){groupBy} * 100 / sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        if isUnResolvedGUID:
            return 'GUID', newQuery, json.dumps(model), thresholdMultiplier
        return 'ERROR_PERCENTAGE', newQuery, json.dumps(model), thresholdMultiplier
    
    # error count ############################
    res = re.search(
        r"^\s*SELECT\s+count\(\*\)\s*FROM\s+TransactionError\s+WHERE\s+" + idRegEx + r"\s*" + r"AND\s*\(`error.expected` IS FALSE OR `error.expected` IS NULL\) AND (?:http\.statusCode|response\.status|httpResponseCode)\s*>=\s*'?500'? EXTRAPOLATE",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        if entityType != "APPLICATION" and entityType != "KEY_TRANSACTION":
            raise ValueError("unhandled entity type " + entityType)

        fragment, labelPairs = makeEsr(entityType, names)
        
        spanKind = 'span_kind=~"server|consumer"'
        
        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''

        model = {}

        httpCodeType = '500' # groupdict.get('httpCodeType')
        if httpCodeType:
            model = {}
            if httpCodeType == '500':
                fragment += ', http_code=~"5.*"'
            else:
                raise ValueError("unhandled httpCodeType " + httpCodeType)

        metric = 'cube_apm_errors_total' if compat == 'legacy' else 'cube_apm_calls_total'

        newQuery = 'sum(increase({metric}{{{fragment}, {spanKind}, status_code="ERROR"}} default 0)){groupBy}'.format(metric=metric, fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        if isUnResolvedGUID:
            return 'GUID', newQuery, json.dumps(model), 1
        return 'ERROR_COUNT', newQuery, json.dumps(model), 1

    # cpu user utilization ############################
    res = re.search(
        r"^\s*SELECT\s+average\s*\(\s*newrelic\.timeslice\.value\s*\)\s*"
        r"(?:AS\s*(?:\w+|''[^']*''|'[^']*'|\"[^\"]*\"))?\s*"
        r"FROM\s+Metric\s+WHERE\s+"
        r"metricTimesliceName\s*=\s*(?P<metricTimesliceName>''[^']+''|'[^']+'|\"[^\"]+\")\s*"
        r"(?:AND\s+)?"
        + idRegEx + r"\s*"
        + facetOptionalRegEx + r"\s*$",
        query,
        flags=re.IGNORECASE,
    )
    if res:
        groupdict = res.groupdict()

        metric_timeslice_name = strip_nr_quoted_literal(groupdict.get("metricTimesliceName") or "")
        if metric_timeslice_name.lower() != "cpu/user/utilization":
            # Not our CPU metric; let other handlers match.
            pass
        else:
            try:
                entityType, names, isUnResolvedGUID = resolveEntityGuids(
                    groupdict.get("idType"), groupdict.get("guid"), groupdict.get("guids"), all_entities
                )
            except Exception as ex:
                return "ERROR", "# %s\n%s" % (ex, query), "{}", 1

            if entityType != "APPLICATION" and entityType != "KEY_TRANSACTION":
                raise ValueError("unhandled entity type " + entityType)

            fragment, labelPairs = makeEsr(entityType, names)
            groupBy = parseFacet(entityType, groupdict.get("facet"))
            groupByStr = " by ({})".format(",".join(groupBy)) if groupBy else ""

            model = {}
            newQuery = 'avg(cube_apm_cpu_utilization{{{fragment}, state="user"}}){groupBy} * 100'.format(
                fragment=fragment,
                groupBy=groupByStr,
            )

            if isUnResolvedGUID:
                return "GUID", newQuery, json.dumps(model), 1
            return "CPU_USER_UTILIZATION", newQuery, json.dumps(model), 1

    # heap memory utilization ############################
    res = re.search(
        r"^\s*SELECT\s+average\s*\(\s*newrelic\.timeslice\.value\s*\)\s*"
        r"(?:AS\s*(?:\w+|''[^']*''|'[^']*'|\"[^\"]*\"))?\s*"
        r"FROM\s+Metric\s+WHERE\s+"
        r"metricTimesliceName\s*=\s*(?P<metricTimesliceName>''[^']+''|'[^']+'|\"[^\"]+\")\s*"
        r"(?:AND\s+)?"
        + idRegEx
        + r"\s*"
        + facetOptionalRegEx
        + r"\s*$",
        query,
        flags=re.IGNORECASE,
    )
    if res:
        groupdict = res.groupdict()

        metric_timeslice_name = strip_nr_quoted_literal(groupdict.get("metricTimesliceName") or "")
        if metric_timeslice_name.lower() != "memory/heap/utilization":
            pass
        else:
            try:
                entityType, names, isUnResolvedGUID = resolveEntityGuids(
                    groupdict.get("idType"), groupdict.get("guid"), groupdict.get("guids"), all_entities
                )
            except Exception as ex:
                return "ERROR", "# %s\n%s" % (ex, query), "{}", 1

            if entityType != "APPLICATION" and entityType != "KEY_TRANSACTION":
                raise ValueError("unhandled entity type " + entityType)

            fragment, labelPairs = makeEsr(entityType, names)
            groupBy = parseFacet(entityType, groupdict.get("facet"))
            groupByStr = " by ({})".format(",".join(groupBy)) if groupBy else ""

            model = {}
            newQuery = (
                'sum(jvm.memory.used{{{fragment}, jvm.memory.type="heap"}}){groupBy} * 100 / '
                'sum(jvm.memory.limit{{{fragment}, jvm.memory.type="heap"}}){groupBy}'
            ).format(fragment=fragment, groupBy=groupByStr)

            if isUnResolvedGUID:
                return "GUID", newQuery, json.dumps(model), 1
            return "MEMORY_HEAP_UTILIZATION", newQuery, json.dumps(model), 1
    
    # avg latency ############################
    res = re.search(
        r"^\s*SELECT\s+average\s*\(\s*apm\.(?P<mType>service|key)\.(?P<mType2>transaction|datastore)\.duration\s*\)\s*(?P<thousand>\*\s*1000)?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    ) or re.search(
        r"^\s*SELECT\s+average\s*\(\s*convert\s*\(\s*apm\.(?P<mType>service|key)\.(?P<mType2>transaction|datastore)\.duration\s*,\s*unit\s*,\s*(?:'|\")(?P<thousand>ms)(?:'|\")\s*\)\s*\)\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
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
            entityType, names, isUnResolvedGUID = resolveEntityGuids('appName' if groupdict.get('lambdaMarker') else groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

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
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''

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
        
        newQuery = 'sum(increase(cube_apm_latency_sum{{{fragment}, {spanKind}}} default 0)){groupBy} * 1000 / sum(increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
        
        # print(newQuery)
        if isUnResolvedGUID:
            return 'GUID', newQuery, json.dumps(model), thresholdMultiplier
        return 'LATENCY_AVERAGE', newQuery, json.dumps(model), thresholdMultiplier
    
    # percentile latency — Transaction WHERE appName AND request.uri ############################
    res = re.search(
        r"^\s*SELECT\s+percentile\s*\(\s*duration\s*,\s*(?P<percentile>[0-9\.]+)\s*\)\s*(?:\*\s*1000)?\s*(?:AS\s+(?:\w+|''[^']*''|'[^']+'|\"[^\"]+\"))?\s+FROM\s+Transaction\s+WHERE\s+"
        r"(?:\(\s*)?`?appName`?\s*=\s*(?P<appNameVal>''[^']+''|'[^']+'|\"[^\"]+\")\s*(?:\)\s*)?\s*(?:AND|and)\s+(?:\(\s*)?`?request\.uri`?\s*=\s*(?P<uriVal>''[^']+''|'[^']+'|\"[^\"]+\")\s*(?:\)\s*)?\s*(?:\s+EXTRAPOLATE)?\s*$",
        query,
        flags=re.IGNORECASE,
    )
    if res:
        groupdict = res.groupdict()
        thresholdMultiplier = 1000
        percentile = groupdict["percentile"]
        app_name = strip_nr_quoted_literal(groupdict["appNameVal"])
        request_uri = strip_nr_quoted_literal(groupdict["uriVal"])
        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids("appName", app_name, None, all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1
        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
        fragment, labelPairs = makeEsr(entityType, names)
        spanKind = 'span_kind=~"server|consumer"'
        groupBy = []
        groupByWithVmrange = groupBy + ["vmrange"]
        groupByWithVmrangeStr = " by ({})".format(",".join(groupByWithVmrange))
        uri_escaped = re.escape(request_uri)
        fragment += ', root_name=~".*{}.*"'.format(uri_escaped)
        model = {
            "model": {
                "type": "quick",
                "calculate": "latency_percentile",
                "value": percentile,
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }
        newQuery = "histogram_quantile({percentile}/100, sum(increase(cube_apm_latency_bucket{{{fragment}, {spanKind}}} default 0)){groupByWithVmrange}) * 1000".format(
            fragment=fragment,
            spanKind=spanKind,
            percentile=percentile,
            groupByWithVmrange=groupByWithVmrangeStr,
        )
        if isUnResolvedGUID:
            return "GUID", newQuery, json.dumps(model), thresholdMultiplier
        return "LATENCY_PERCENTILE", newQuery, json.dumps(model), thresholdMultiplier

    # percentile latency ############################
    # Optional * 1000 and AS alias (NRQL often uses "as Average"); optional extra AND (...) e.g. transactionSubType
    transactionWhereTailRegEx = (
        r"\s*"
        + idRegEx
        + r"\s*"
        + transactionTypeOptionalRegEx
        + r"\s*(?:AND\s*\([^)]+\)\s*)*"
        + facetOptionalRegEx
        + r"\s*(?:\s+EXTRAPOLATE)?\s*$"
    )
    res = re.search(
        r"^\s*SELECT\s+percentile\s*\(\s*duration\s*,\s*(?P<percentile>[0-9\.]+)\s*\)\s*(?:\*\s*1000)?\s*(?:AS\s+(?:\w+|''[^']*''|'[^']+'|\"[^\"]+\"))?\s+FROM\s+Transaction\s+WHERE\s+"
        + transactionWhereTailRegEx,
        query,
        flags=re.IGNORECASE,
    )
    if res:
        groupdict = res.groupdict()

        thresholdMultiplier = 1000

        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
            
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        # groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        groupByWithVmrange = groupBy + ['vmrange']
        groupByWithVmrangeStr = ' by ({})'.format(','.join(groupByWithVmrange))

        percentile = groupdict['percentile']

        model = {
            "model": {
                "type": "quick",
                "calculate": "latency_percentile",
                "value": percentile,
                "labelPairs": labelPairs,
                "groupBy": groupBy,
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
        
        newQuery = 'histogram_quantile({percentile}/100, sum(increase(cube_apm_latency_bucket{{{fragment}, {spanKind}}} default 0)){groupByWithVmrange}) * 1000'.format(fragment=fragment, spanKind=spanKind, percentile=percentile, groupByWithVmrange=groupByWithVmrangeStr)
        
        # print(newQuery)

        if isUnResolvedGUID:
            return 'GUID', newQuery, json.dumps(model), thresholdMultiplier
        return 'LATENCY_PERCENTILE', newQuery, json.dumps(model), thresholdMultiplier

    # http status percentage (NRQL percentage(count(*), WHERE ...)) ############################
    res = re.search(
        r"^\s*SELECT\s+percentage\s*\(\s*count\s*\(\s*\*\s*\)\s*,\s*WHERE\s+(?P<cond>.+?)\)\s*FROM\s+Transaction\s+WHERE\s+" + idRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names, isUnResolvedGUID = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)

        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        cond = groupdict.get('cond') or ''

        httpMatchers = []
        # Support ranges like: http.statusCode >= '400' AND http.statusCode < '500' -> http_code=~"4.*"
        lower = re.search(r"http\.statusCode\s*>?=\s*'(\d{3})'", cond, flags=re.IGNORECASE)
        upper = re.search(r"http\.statusCode\s*<\s*'(\d{3})'", cond, flags=re.IGNORECASE)
        if lower and upper:
            low = lower.group(1)
            up = upper.group(1)
            try:
                low_i = int(low)
                up_i = int(up)
                if low_i % 100 == 0 and up_i - low_i == 100:
                    httpMatchers.append('http_code=~"' + re.escape(low[0]) + '.*"')
                elif up_i > low_i:
                    codes = '|'.join(str(c) for c in range(low_i, up_i))
                    httpMatchers.append('http_code=~"(' + codes + ')"')
            except Exception:
                pass
        # Support LIKE '4%' / '5%' / '400' forms
        for m in re.finditer(r"http\.statusCode\s+LIKE\s+'(\d{1,3})(%)?'", cond, flags=re.IGNORECASE):
            prefix = m.group(1)
            isPrefix = bool(m.group(2))
            if isPrefix:
                httpMatchers.append('http_code=~"' + re.escape(prefix) + '.*"')
            else:
                httpMatchers.append('http_code="' + prefix + '"')
        # Support equality http.statusCode = '401'
        for m in re.finditer(r"http\.statusCode\s*=\s*'(\d{3})'", cond, flags=re.IGNORECASE):
            httpMatchers.append('http_code="' + m.group(1) + '"')
        # Support inequality http.statusCode != '400'
        for m in re.finditer(r"http\.statusCode\s*!=\s*'(\d{3})'", cond, flags=re.IGNORECASE):
            httpMatchers.append('http_code!="' + m.group(1) + '"')

        httpFilter = (', ' + ', '.join(httpMatchers)) if httpMatchers else ''

        # Build percentage: numerator (matching) over denominator (all) * 100
        newQuery = 'sum(increase(cube_apm_errors_total{{{fragment}, {spanKind}, status_code="ERROR"{httpFilter}}} default 0)) * 100 / sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0))'.format(
            fragment=fragment, spanKind=spanKind, httpFilter=httpFilter
        )

        model = {}

        if isUnResolvedGUID:
            return 'GUID', newQuery, json.dumps(model), 1
        return 'HTTP_STATUS_PERCENTAGE', newQuery, json.dumps(model), 1

    # AWS metrics ############################
    # res = re.search(
    #     r"^\s*SELECT\s+average\s*\(\s*`aws\.(?P<awsService>\w+)\.(?P<awsMetric>\w+)`\s*\)\s*FROM\s+Metric\s+FACET\s*`aws\.(?P=awsService)\.(?P<awsDimension>)`\s*WHERE\s+`collector.name`\s*=\s*['\"][\w-]+['\"]\s*AND\s*`aws\.accountId`\s*=\s*['\"]' AND `aws.Arn` LIKE 'arn:aws:rds:ap-south-1:411342004333:db:checkout-prod-readonly'\s*$",
    #     query, flags=re.IGNORECASE
    # )
    # if res:
    #     groupdict = res.groupdict()

    # trimmedquery = query.replace('\n', '') + '\n'
    # print(trimmedquery)
    

    return "UNHANDLED", query, '{}', 1


def migrate(src_acct_id, compat, mode):
    policies_list = store.load_json_file(src_acct_id, store.ALERT_POLICIES_DIR, 'alert_policies.json')
    all_policies = { policy['id'] : policy for policy in policies_list['policies'] }
    all_entities = store.load_json_from_file('output', '%s_entities_extended.json' % str(src_acct_id))
    all_alert_conditions = store.load_json_file(src_acct_id, store.ALERT_POLICIES_DIR, 'alert_conditions.json')

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
        qType, query, config, thresholdMultiplier = mapQuery(query, all_entities, compat)
        if qType in ["UNHANDLED", "ERROR", "GUID"]:
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
(account_id, datasource, kind, name, `interval`, expr, expr2, `for`, labels, annotations, status, config, receiver, repeat_interval, mute, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval, '{}')
        else:
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, "interval", expr, expr2, "for", labels, annotations, status, config, receiver, repeat_interval, mute, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval, '{}')
        
        print(statement)

    print('\n\n-- app conditions --\n\n')
    for condition in all_alert_conditions['app']:
        datasource = 'prometheus'
        kind = 'static'
        name = condition['name']
        interval = 60
        # expr = condition['nrql']['query']
        expr2 = ''
        # forValue = condition['terms']['duration'] * 60
        labels = json.dumps({"group": 'Default Group'}) # we don't get policy info in app alert conditions
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

        entities = condition.get('entities')
        if not entities:
            print('-- unhandled app alert: ' + name)
            continue

        # try:
        entityType, names, isUnResolvedGUID = resolveEntityGuids('appId', None, ','.join(entities), all_entities)
        # except Exception as ex:
            # return "ERROR", "# %s\n%s" % (ex, query), '{}', 1

        type = condition['type']
        metric = condition.get('metric')

        if type == 'apm_app_metric':
            if entityType != "APPLICATION":
                    raise ValueError("unhandled situation in app alert: " + str(condition['id']))

            fragment, labelPairs = makeEsr(entityType, names)
            spanKind = 'span_kind=~"server|consumer"'
            groupBy = ['service']
            groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''

            if metric == 'error_percentage':
                model = {
                    "model": {
                        "type": "quick",
                        "calculate": "error_percentage",
                        "value": "0",
                        "labelPairs": labelPairs,
                        "groupBy": groupBy,
                    },
                }

                query = 'sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}, status_code="ERROR"}} default 0)){groupBy} * 100 / sum(increase(cube_apm_calls_total{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
            elif metric == 'response_time_web':
                # transactionType = 'Web'
                model = {}
                fragment += ', root_name=~"WebTransaction/.*"'
                
                # do not multiply by 1000 as the threshold is in seconds
                query = 'sum(increase(cube_apm_latency_sum{{{fragment}, {spanKind}}} default 0)){groupBy} / sum(increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0)){groupBy}'.format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr)
            elif metric == 'apdex':
                model = {}
                groupByWithVmrange = groupBy + ['vmrange']
                groupByWithVmrangeStr = ' by ({})'.format(','.join(groupByWithVmrange))

                query = """0.5 *
        (
        histogram_share(0.5, sum{groupByWithVmrange} (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
        +
        histogram_share(2.0, sum{groupByWithVmrange} (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
        )
        * sum{groupBy} (increase(cube_apm_latency_count{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0))
        / sum{groupBy} (increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0))""".format(fragment=fragment, spanKind=spanKind, groupBy=groupByStr, groupByWithVmrange=groupByWithVmrangeStr)
            else:
                print('-- unhandled app alert: ' + name)
                continue

            # print(query)
            if isUnResolvedGUID:
                name = "[GUID] {}".format(name)
            config = json.dumps(model)
        elif type == 'apm_app_metric_baseline':
            print('-- unhandled app alert: ' + name)
            continue
        elif type == 'apm_kt_metric':
            print('-- unhandled app alert: ' + name)
            continue
        elif type == 'apm_response_time_percentile':
            if entityType != "APPLICATION":
                raise ValueError("unhandled situation in app alert: " + str(condition['id']))
                
            fragment, labelPairs = makeEsr(entityType, names)

            spanKind = 'span_kind=~"server|consumer"'

            groupBy = ['service']
            # groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
            groupByWithVmrange = groupBy + ['vmrange']
            groupByWithVmrangeStr = ' by ({})'.format(','.join(groupByWithVmrange))

            percentile = condition['percentile_value']

            # transactionType = 'Web'
            model = {}
            fragment += ', root_name=~"WebTransaction/.*"'

            # do not multiply by 1000 as the threshold is in seconds
            query = 'histogram_quantile({percentile}/100, sum(increase(cube_apm_latency_bucket{{{fragment}, {spanKind}}} default 0)){groupByWithVmrange})'.format(fragment=fragment, spanKind=spanKind, percentile=percentile, groupByWithVmrange=groupByWithVmrangeStr)
            
            # print(query)
            if isUnResolvedGUID:
                name = "[GUID] {}".format(name)

            config = json.dumps(model)
        elif type == 'apm_jvm_metric':
            if entityType != "APPLICATION":
                raise ValueError("unhandled situation in app alert: " + condition['id'])

            fragment, labelPairs = makeEsr(entityType, names, 'service.name')
            if metric == 'cpu_utilization_time':
                # "SELECT filter(average(newrelic.timeslice.value) * 100, WHERE metricTimesliceName = 'CPU/User/Utilization') OR 0 FROM Metric WHERE appId IN (1404940060) AND metricTimesliceName IN ('CPU/User/Utilization', 'Agent/MetricsReported/count') FACET appId, realAgentId, host"
                query = 'avg(cube_apm_cpu_utilization{{{fragment}, state="user"}}) by (service.name, host.name) * 100'.format(fragment=fragment)
                model = {}
                config = json.dumps(model)
            elif metric == 'heap_memory_usage':
                # SELECT filter(average(newrelic.timeslice.value) * 100, WHERE metricTimesliceName = 'Memory/Heap/Utilization') OR 0 FROM Metric WHERE appId IN (107184768) AND metricTimesliceName IN ('Memory/Heap/Utilization', 'Agent/MetricsReported/count') FACET appId, realAgentId, host
                query = 'sum(jvm.memory.used{{{fragment}, jvm.memory.type="heap"}}) by (service.name, host.name) * 100 / sum(jvm.memory.limit{{{fragment}, jvm.memory.type="heap"}}) by (service.name, host.name)'.format(fragment=fragment)
                model = {}
                config = json.dumps(model)
            else:
                print('-- unhandled app alert: ' + name)
                continue
        else:
            raise ValueError("unhandled app condition type " + type)

        terms = condition['terms']
        if len(terms) == 1:
            forValue = int(terms[0]['duration']) * 60
            threshold = terms[0]['threshold']
            operator = mapOperator[terms[0]['operator']]

            expr = "({query}) {operator} {threshold}".format(query=query, operator=operator, threshold=threshold)
        elif len(terms) == 2:
            if terms[0]['priority'] == 'warning':
                warningTerms = terms[0]
            elif terms[0]['priority'] == 'critical':
                criticalTerms = terms[0]
            else:
                raise ValueError("unhandled terms.priority for condition id " + condition["id"])
            if terms[1]['priority'] == 'warning':
                warningTerms = terms[1]
            elif terms[1]['priority'] == 'critical':
                criticalTerms = terms[1]
            else:
                raise ValueError("unhandled terms.priority for condition id " + condition["id"])

            if not warningTerms or not criticalTerms:
                raise ValueError("unhandled terms for condition id " + condition["id"])

            forValue = int(warningTerms['duration']) * 60
            wThreshold = warningTerms['threshold']
            wOperator = mapOperator[warningTerms['operator']]
            cThreshold = criticalTerms['threshold']
            cOperator = mapOperator[criticalTerms['operator']]

            expr = "({query}) {operator} {threshold}".format(query=query, operator=wOperator, threshold=wThreshold)
            expr2 = "({query}) {operator} {threshold}".format(query=query, operator=cOperator, threshold=cThreshold)

        if mode == 'mysql':
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, `interval`, expr, expr2, `for`, labels, annotations, status, config, receiver, repeat_interval, mute, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval, '{}')
        else:
            statement = """INSERT INTO alert_rules
(account_id, datasource, kind, name, "interval", expr, expr2, "for", labels, annotations, status, config, receiver, repeat_interval, mute, created_at, updated_at)
VALUES
(1, '{}', '{}', '{}', {}, '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', {}, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
""".format(datasource, kind, squoteSQL(name, mode), interval, squoteSQL(expr, mode), squoteSQL(expr2, mode), forValue, squoteSQL(labels, mode), squoteSQL(annotations, mode), squoteSQL(status, mode), squoteSQL(config, mode), squoteSQL(receiver, mode), repeat_interval, '{}')
        
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
        '--compat',
        '--compat',
        nargs=1,
        type=str,
        required=True,
        help='legacy or modern',
        dest='compat'
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
    parser = create_argument_parser()
    args = parser.parse_args()
    
    migrate(args.source_account_id[0], args.compat[0], args.mode[0])


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
