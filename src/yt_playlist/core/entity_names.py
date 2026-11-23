"""Account field names in stored documents and external payloads."""


def account_document(value):
    """Decode a historical account field without rewriting its retained document."""
    if isinstance(value,dict) and 'identity_id' in value:
        if 'account_id' in value:
            raise ValueError('Account document supplies competing account fields')
        return {('account_id' if key=='identity_id' else key):item for key,item in value.items()}
    return value


def external_account_document(value):
    return {('identity_id' if key=='account_id' else key):item for key,item in value.items()} if isinstance(value,dict) else value
