def pytest_addoption(parser):
    parser.addoption('--schema-source-db', help='Offline database to copy for populated schema checks')
    parser.addoption('--schema-upgraded-db', help='Retained upgraded output to copy when resuming acceptance checks')
    parser.addoption('--schema-evidence-dir', help='Listening evidence directory to copy into the isolated library')
    parser.addoption('--schema-recovery-db', help='Resume recovery in a retained .pytest_cache test library in place')
    parser.addoption('--schema-reinventory', action='store_true',
                     help='Reopen the inventory fence and replay one actual offer owner in an isolated recovery library')
    parser.addoption('--schema-metadata-startup', action='store_true',
                     help='Copy all owned inputs and inspect HTTP/browser availability during metadata conversion')
    parser.addoption('--schema-metadata-converted-db',
                     help='Copy a retained converted test fixture for metadata continuation checks')
