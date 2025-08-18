import inspect
import json

from a2a import types as a2a_types

print('Introspecting a2a.types names...')
names = dir(a2a_types)
print(f'Total names: {len(names)}')
for n in sorted(names):
    if 'Request' in n or 'Message' in n or 'Task' in n:
        print('  name:', n)

# Collect request-like classes
methods = []
for name, obj in inspect.getmembers(a2a_types):
    if inspect.isclass(obj) and name.endswith('Request'):
        method_value = getattr(obj, 'method', None)
        try:
            fields = list(getattr(obj, 'model_fields', {}).keys())
        except Exception:
            fields = []
        methods.append({'class': name, 'method': method_value, 'fields': fields})

print('\nRaw request class scan:')
print(json.dumps(methods, indent=2, default=str))

print('\nLikely JSON-RPC methods (have non-null method attr):')
for m in methods:
    if m['method']:
        print(' -', m['method'], '->', m['class'], 'fields=', m['fields'])

print('\nCandidates containing message field:')
for m in methods:
    if any(f in ('message','messages') for f in m['fields']):
        print(' -', m['class'], 'method=', m['method'], 'fields=', m['fields'])

print('\nAttempting to extract Literal method values:')
from typing import get_args, get_origin
for name, obj in inspect.getmembers(a2a_types):
    if inspect.isclass(obj) and name.endswith('Request'):
        try:
            field = obj.model_fields.get('method')
        except Exception:
            field = None
        if field is None:
            continue
        ann = getattr(field, 'annotation', None)
        values = []
        if ann is not None and get_origin(ann) is None and str(ann).startswith("typing.Literal"):
            # fallback parsing
            pass
        if ann is not None and getattr(ann, '__origin__', None) is not None:
            if 'Literal' in str(ann):
                try:
                    values = list(get_args(ann))
                except Exception:
                    values = []
        # Another attempt: field.repr could contain choices
        if not values:
            # Heuristic: create instance to trigger validation error with sentinel value
            try:
                dummy = { 'id':'x','jsonrpc':'2.0','method':'__dummy__' }
                obj(**dummy)
            except Exception as e:
                msg = str(e)
                if 'Input should be' in msg and 'agent/' in msg:
                    # extract 'agent/...'
                    import re
                    found = re.findall(r"'agent/[A-Za-z]+'", msg)
                    values.extend([f.strip("'") for f in found])
        if values:
            print(f' - {name}: {values}')

print('\nBrute-force derive expected method literals from validation errors:')
import re
method_map = {}
for name, obj in inspect.getmembers(a2a_types):
    if inspect.isclass(obj) and name.endswith('Request'):
        base_payload = { 'id':'x','jsonrpc':'2.0','method':'__dummy__' }
        # some requests require params; supply empty dict
        if 'params' in getattr(obj, 'model_fields', {}):
            base_payload['params'] = {}
        try:
            obj(**base_payload)  # expect failure
        except Exception as e:
            msg = str(e)
            # pattern: Input should be 'agent/...' or 'task/...'
            m = re.search(r"Input should be '([^']+)'", msg)
            if m:
                expected = m.group(1)
                method_map[name] = expected
                print(f' - {name} expects method={expected}')

print('\nDerived method list:')
for cls, method in sorted(method_map.items(), key=lambda kv: kv[1]):
    print(f' {method} -> {cls}')

# Inspect params schema for SendStreamingMessageRequest
SSR = getattr(a2a_types, 'SendStreamingMessageRequest', None)
if SSR:
    print('\nSendStreamingMessageRequest param model fields:')
    pf = SSR.model_fields.get('params')
    ann = getattr(pf, 'annotation', None)
    print(' params annotation:', ann)
    # Try to instantiate underlying params type if pydantic model
    try:
        params_type = ann
        fields = getattr(params_type, 'model_fields', {})
        print(' param fields:', list(fields.keys()))
        for fname, f in fields.items():
            print('  -', fname, 'annotation=', getattr(f, 'annotation', None))
    except Exception as e:
        print(' could not introspect params type:', e)
