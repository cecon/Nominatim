"""
Command-line interface to the Nominatim functions for import, update,
database administration and querying.
"""
import datetime as dt
import os
import socket
import sys
import time
import argparse
import logging
from pathlib import Path

from .config import Configuration
from .tools.exec_utils import run_legacy_script, run_api_script, run_php_server
from .db.connection import connect
from .db import status
from .errors import UsageError

LOG = logging.getLogger()

def _num_system_cpus():
    try:
        cpus = len(os.sched_getaffinity(0))
    except NotImplementedError:
        cpus = None

    return cpus or os.cpu_count()


class CommandlineParser:
    """ Wraps some of the common functions for parsing the command line
        and setting up subcommands.
    """
    def __init__(self, prog, description):
        self.parser = argparse.ArgumentParser(
            prog=prog,
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter)

        self.subs = self.parser.add_subparsers(title='available commands',
                                               dest='subcommand')

        # Arguments added to every sub-command
        self.default_args = argparse.ArgumentParser(add_help=False)
        group = self.default_args.add_argument_group('Default arguments')
        group.add_argument('-h', '--help', action='help',
                           help='Show this help message and exit')
        group.add_argument('-q', '--quiet', action='store_const', const=0,
                           dest='verbose', default=1,
                           help='Print only error messages')
        group.add_argument('-v', '--verbose', action='count', default=1,
                           help='Increase verboseness of output')
        group.add_argument('--project-dir', metavar='DIR', default='.',
                           help='Base directory of the Nominatim installation (default:.)')
        group.add_argument('-j', '--threads', metavar='NUM', type=int,
                           help='Number of parallel threads to use')


    def add_subcommand(self, name, cmd):
        """ Add a subcommand to the parser. The subcommand must be a class
            with a function add_args() that adds the parameters for the
            subcommand and a run() function that executes the command.
        """
        parser = self.subs.add_parser(name, parents=[self.default_args],
                                      help=cmd.__doc__.split('\n', 1)[0],
                                      description=cmd.__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter,
                                      add_help=False)
        parser.set_defaults(command=cmd)
        cmd.add_args(parser)

    def run(self, **kwargs):
        """ Parse the command line arguments of the program and execute the
            appropriate subcommand.
        """
        args = self.parser.parse_args(args=kwargs.get('cli_args'))

        if args.subcommand is None:
            self.parser.print_help()
            return 1

        for arg in ('module_dir', 'osm2pgsql_path', 'phplib_dir', 'data_dir', 'phpcgi_path'):
            setattr(args, arg, Path(kwargs[arg]))
        args.project_dir = Path(args.project_dir).resolve()

        logging.basicConfig(stream=sys.stderr,
                            format='%(asctime)s: %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=max(4 - args.verbose, 1) * 10)

        args.config = Configuration(args.project_dir, args.data_dir / 'settings')

        log = logging.getLogger()
        log.warning('Using project directory: %s', str(args.project_dir))

        try:
            return args.command.run(args)
        except UsageError as exception:
            if log.isEnabledFor(logging.DEBUG):
                raise # use Python's exception printing
            log.fatal('FATAL: %s', exception)

        # If we get here, then execution has failed in some way.
        return 1


def _osm2pgsql_options_from_args(args, default_cache, default_threads):
    """ Set up the stanadrd osm2pgsql from the command line arguments.
    """
    return dict(osm2pgsql=args.osm2pgsql_path,
                osm2pgsql_cache=args.osm2pgsql_cache or default_cache,
                osm2pgsql_style=args.config.get_import_style_file(),
                threads=args.threads or default_threads,
                dsn=args.config.get_libpq_dsn(),
                flatnode_file=args.config.FLATNODE_FILE)

##### Subcommand classes
#
# Each class needs to implement two functions: add_args() adds the CLI parameters
# for the subfunction, run() executes the subcommand.
#
# The class documentation doubles as the help text for the command. The
# first line is also used in the summary when calling the program without
# a subcommand.
#
# No need to document the functions each time.
# pylint: disable=C0111
# Using non-top-level imports to make pyosmium optional for replication only.
# pylint: disable=E0012,C0415


class SetupAll:
    """\
    Create a new Nominatim database from an OSM file.
    """

    @staticmethod
    def add_args(parser):
        group_name = parser.add_argument_group('Required arguments')
        group = group_name.add_mutually_exclusive_group(required=True)
        group.add_argument('--osm-file',
                           help='OSM file to be imported.')
        group.add_argument('--continue', dest='continue_at',
                           choices=['load-data', 'indexing', 'db-postprocess'],
                           help='Continue an import that was interrupted')
        group = parser.add_argument_group('Optional arguments')
        group.add_argument('--osm2pgsql-cache', metavar='SIZE', type=int,
                           help='Size of cache to be used by osm2pgsql (in MB)')
        group.add_argument('--reverse-only', action='store_true',
                           help='Do not create tables and indexes for searching')
        group.add_argument('--enable-debug-statements', action='store_true',
                           help='Include debug warning statements in SQL code')
        group.add_argument('--no-partitions', action='store_true',
                           help="""Do not partition search indices
                                   (speeds up import of single country extracts)""")
        group.add_argument('--no-updates', action='store_true',
                           help="""Do not keep tables that are only needed for
                                   updating the database later""")
        group = parser.add_argument_group('Expert options')
        group.add_argument('--ignore-errors', action='store_true',
                           help='Continue import even when errors in SQL are present')
        group.add_argument('--index-noanalyse', action='store_true',
                           help='Do not perform analyse operations during index')


    @staticmethod
    def run(args):
        params = ['setup.php']
        if args.osm_file:
            params.extend(('--all', '--osm-file', args.osm_file))
        else:
            if args.continue_at == 'load-data':
                params.append('--load-data')
            if args.continue_at in ('load-data', 'indexing'):
                params.append('--index')
            params.extend(('--create-search-indices', '--create-country-names',
                           '--setup-website'))
        if args.osm2pgsql_cache:
            params.extend(('--osm2pgsql-cache', args.osm2pgsql_cache))
        if args.reverse_only:
            params.append('--reverse-only')
        if args.enable_debug_statements:
            params.append('--enable-debug-statements')
        if args.no_partitions:
            params.append('--no-partitions')
        if args.no_updates:
            params.append('--drop')
        if args.ignore_errors:
            params.append('--ignore-errors')
        if args.index_noanalyse:
            params.append('--index-noanalyse')

        return run_legacy_script(*params, nominatim_env=args)


class SetupFreeze:
    """\
    Make database read-only.

    About half of data in the Nominatim database is kept only to be able to
    keep the data up-to-date with new changes made in OpenStreetMap. This
    command drops all this data and only keeps the part needed for geocoding
    itself.

    This command has the same effect as the `--no-updates` option for imports.
    """

    @staticmethod
    def add_args(parser):
        pass # No options

    @staticmethod
    def run(args):
        return run_legacy_script('setup.php', '--drop', nominatim_env=args)


class SetupSpecialPhrases:
    """\
    Maintain special phrases.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Input arguments')
        group.add_argument('--from-wiki', action='store_true',
                           help='Pull special phrases from the OSM wiki.')
        group = parser.add_argument_group('Output arguments')
        group.add_argument('-o', '--output', default='-',
                           help="""File to write the preprocessed phrases to.
                                   If omitted, it will be written to stdout.""")

    @staticmethod
    def run(args):
        if args.output != '-':
            raise NotImplementedError('Only output to stdout is currently implemented.')
        return run_legacy_script('specialphrases.php', '--wiki-import', nominatim_env=args)


class UpdateReplication:
    """\
    Update the database using an online replication service.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Arguments for initialisation')
        group.add_argument('--init', action='store_true',
                           help='Initialise the update process')
        group.add_argument('--no-update-functions', dest='update_functions',
                           action='store_false',
                           help="""Do not update the trigger function to
                                   support differential updates.""")
        group = parser.add_argument_group('Arguments for updates')
        group.add_argument('--check-for-updates', action='store_true',
                           help='Check if new updates are available and exit')
        group.add_argument('--once', action='store_true',
                           help="""Download and apply updates only once. When
                                   not set, updates are continuously applied""")
        group.add_argument('--no-index', action='store_false', dest='do_index',
                           help="""Do not index the new data. Only applicable
                                   together with --once""")
        group.add_argument('--osm2pgsql-cache', metavar='SIZE', type=int,
                           help='Size of cache to be used by osm2pgsql (in MB)')
        group = parser.add_argument_group('Download parameters')
        group.add_argument('--socket-timeout', dest='socket_timeout', type=int, default=60,
                           help='Set timeout for file downloads.')

    @staticmethod
    def _init_replication(args):
        from .tools import replication, refresh

        socket.setdefaulttimeout(args.socket_timeout)

        LOG.warning("Initialising replication updates")
        conn = connect(args.config.get_libpq_dsn())
        replication.init_replication(conn, base_url=args.config.REPLICATION_URL)
        if args.update_functions:
            LOG.warning("Create functions")
            refresh.create_functions(conn, args.config, args.data_dir,
                                     True, False)
        conn.close()
        return 0


    @staticmethod
    def _check_for_updates(args):
        from .tools import replication

        conn = connect(args.config.get_libpq_dsn())
        ret = replication.check_for_updates(conn, base_url=args.config.REPLICATION_URL)
        conn.close()
        return ret

    @staticmethod
    def _report_update(batchdate, start_import, start_index):
        def round_time(delta):
            return dt.timedelta(seconds=int(delta.total_seconds()))

        end = dt.datetime.now(dt.timezone.utc)
        LOG.warning("Update completed. Import: %s. %sTotal: %s. Remaining backlog: %s.",
                    round_time((start_index or end) - start_import),
                    "Indexing: {} ".format(round_time(end - start_index))
                    if start_index else '',
                    round_time(end - start_import),
                    round_time(end - batchdate))

    @staticmethod
    def _update(args):
        from .tools import replication
        from .indexer.indexer import Indexer

        params = _osm2pgsql_options_from_args(args, 2000, 1)
        params.update(base_url=args.config.REPLICATION_URL,
                      update_interval=args.config.get_int('REPLICATION_UPDATE_INTERVAL'),
                      import_file=args.project_dir / 'osmosischange.osc',
                      max_diff_size=args.config.get_int('REPLICATION_MAX_DIFF'),
                      indexed_only=not args.once)

        # Sanity check to not overwhelm the Geofabrik servers.
        if 'download.geofabrik.de'in params['base_url']\
           and params['update_interval'] < 86400:
            LOG.fatal("Update interval too low for download.geofabrik.de.\n"
                      "Please check install documentation "
                      "(https://nominatim.org/release-docs/latest/admin/Import-and-Update#"
                      "setting-up-the-update-process).")
            raise UsageError("Invalid replication update interval setting.")

        if not args.once:
            if not args.do_index:
                LOG.fatal("Indexing cannot be disabled when running updates continuously.")
                raise UsageError("Bad argument '--no-index'.")
            recheck_interval = args.config.get_int('REPLICATION_RECHECK_INTERVAL')

        while True:
            conn = connect(args.config.get_libpq_dsn())
            start = dt.datetime.now(dt.timezone.utc)
            state = replication.update(conn, params)
            if state is not replication.UpdateState.NO_CHANGES:
                status.log_status(conn, start, 'import')
            batchdate, _, _ = status.get_status(conn)
            conn.close()

            if state is not replication.UpdateState.NO_CHANGES and args.do_index:
                index_start = dt.datetime.now(dt.timezone.utc)
                indexer = Indexer(args.config.get_libpq_dsn(),
                                  args.threads or 1)
                indexer.index_boundaries(0, 30)
                indexer.index_by_rank(0, 30)

                conn = connect(args.config.get_libpq_dsn())
                status.set_indexed(conn, True)
                status.log_status(conn, index_start, 'index')
                conn.close()
            else:
                index_start = None

            if LOG.isEnabledFor(logging.WARNING):
                UpdateReplication._report_update(batchdate, start, index_start)

            if args.once:
                break

            if state is replication.UpdateState.NO_CHANGES:
                LOG.warning("No new changes. Sleeping for %d sec.", recheck_interval)
                time.sleep(recheck_interval)

        return state.value

    @staticmethod
    def run(args):
        try:
            import osmium # pylint: disable=W0611
        except ModuleNotFoundError:
            LOG.fatal("pyosmium not installed. Replication functions not available.\n"
                      "To install pyosmium via pip: pip3 install osmium")
            return 1

        if args.init:
            return UpdateReplication._init_replication(args)

        if args.check_for_updates:
            return UpdateReplication._check_for_updates(args)

        return UpdateReplication._update(args)

class UpdateAddData:
    """\
    Add additional data from a file or an online source.

    Data is only imported, not indexed. You need to call `nominatim-update index`
    to complete the process.
    """

    @staticmethod
    def add_args(parser):
        group_name = parser.add_argument_group('Source')
        group = group_name.add_mutually_exclusive_group(required=True)
        group.add_argument('--file', metavar='FILE',
                           help='Import data from an OSM file')
        group.add_argument('--diff', metavar='FILE',
                           help='Import data from an OSM diff file')
        group.add_argument('--node', metavar='ID', type=int,
                           help='Import a single node from the API')
        group.add_argument('--way', metavar='ID', type=int,
                           help='Import a single way from the API')
        group.add_argument('--relation', metavar='ID', type=int,
                           help='Import a single relation from the API')
        group.add_argument('--tiger-data', metavar='DIR',
                           help='Add housenumbers from the US TIGER census database.')
        group = parser.add_argument_group('Extra arguments')
        group.add_argument('--use-main-api', action='store_true',
                           help='Use OSM API instead of Overpass to download objects')

    @staticmethod
    def run(args):
        if args.tiger_data:
            os.environ['NOMINATIM_TIGER_DATA_PATH'] = args.tiger_data
            return run_legacy_script('setup.php', '--import-tiger-data', nominatim_env=args)

        params = ['update.php']
        if args.file:
            params.extend(('--import-file', args.file))
        elif args.diff:
            params.extend(('--import-diff', args.diff))
        elif args.node:
            params.extend(('--import-node', args.node))
        elif args.way:
            params.extend(('--import-way', args.way))
        elif args.relation:
            params.extend(('--import-relation', args.relation))
        if args.use_main_api:
            params.append('--use-main-api')
        return run_legacy_script(*params, nominatim_env=args)


class UpdateIndex:
    """\
    Reindex all new and modified data.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Filter arguments')
        group.add_argument('--boundaries-only', action='store_true',
                           help="""Index only administrative boundaries.""")
        group.add_argument('--no-boundaries', action='store_true',
                           help="""Index everything except administrative boundaries.""")
        group.add_argument('--minrank', '-r', type=int, metavar='RANK', default=0,
                           help='Minimum/starting rank')
        group.add_argument('--maxrank', '-R', type=int, metavar='RANK', default=30,
                           help='Maximum/finishing rank')

    @staticmethod
    def run(args):
        from .indexer.indexer import Indexer

        indexer = Indexer(args.config.get_libpq_dsn(),
                          args.threads or _num_system_cpus() or 1)

        if not args.no_boundaries:
            indexer.index_boundaries(args.minrank, args.maxrank)
        if not args.boundaries_only:
            indexer.index_by_rank(args.minrank, args.maxrank)

        if not args.no_boundaries and not args.boundaries_only \
           and args.minrank == 0 and args.maxrank == 30:
            conn = connect(args.config.get_libpq_dsn())
            status.set_indexed(conn, True)
            conn.close()

        return 0


class UpdateRefresh:
    """\
    Recompute auxiliary data used by the indexing process.

    These functions must not be run in parallel with other update commands.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Data arguments')
        group.add_argument('--postcodes', action='store_true',
                           help='Update postcode centroid table')
        group.add_argument('--word-counts', action='store_true',
                           help='Compute frequency of full-word search terms')
        group.add_argument('--address-levels', action='store_true',
                           help='Reimport address level configuration')
        group.add_argument('--functions', action='store_true',
                           help='Update the PL/pgSQL functions in the database')
        group.add_argument('--wiki-data', action='store_true',
                           help='Update Wikipedia/data importance numbers.')
        group.add_argument('--importance', action='store_true',
                           help='Recompute place importances (expensive!)')
        group.add_argument('--website', action='store_true',
                           help='Refresh the directory that serves the scripts for the web API')
        group = parser.add_argument_group('Arguments for function refresh')
        group.add_argument('--no-diff-updates', action='store_false', dest='diffs',
                           help='Do not enable code for propagating updates')
        group.add_argument('--enable-debug-statements', action='store_true',
                           help='Enable debug warning statements in functions')

    @staticmethod
    def run(args):
        from .tools import refresh

        if args.postcodes:
            LOG.warning("Update postcodes centroid")
            conn = connect(args.config.get_libpq_dsn())
            refresh.update_postcodes(conn, args.data_dir)
            conn.close()

        if args.word_counts:
            LOG.warning('Recompute frequency of full-word search terms')
            conn = connect(args.config.get_libpq_dsn())
            refresh.recompute_word_counts(conn, args.data_dir)
            conn.close()

        if args.address_levels:
            cfg = Path(args.config.ADDRESS_LEVEL_CONFIG)
            LOG.warning('Updating address levels from %s', cfg)
            conn = connect(args.config.get_libpq_dsn())
            refresh.load_address_levels_from_file(conn, cfg)
            conn.close()

        if args.functions:
            LOG.warning('Create functions')
            conn = connect(args.config.get_libpq_dsn())
            refresh.create_functions(conn, args.config, args.data_dir,
                                     args.diffs, args.enable_debug_statements)
            conn.close()

        if args.wiki_data:
            run_legacy_script('setup.php', '--import-wikipedia-articles',
                              nominatim_env=args, throw_on_fail=True)
        # Attention: importance MUST come after wiki data import.
        if args.importance:
            run_legacy_script('update.php', '--recompute-importance',
                              nominatim_env=args, throw_on_fail=True)
        if args.website:
            run_legacy_script('setup.php', '--setup-website',
                              nominatim_env=args, throw_on_fail=True)

        return 0


class AdminCheckDatabase:
    """\
    Check that the database is complete and operational.
    """

    @staticmethod
    def add_args(parser):
        pass # No options

    @staticmethod
    def run(args):
        return run_legacy_script('check_import_finished.php', nominatim_env=args)


class AdminWarm:
    """\
    Warm database caches for search and reverse queries.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Target arguments')
        group.add_argument('--search-only', action='store_const', dest='target',
                           const='search',
                           help="Only pre-warm tables for search queries")
        group.add_argument('--reverse-only', action='store_const', dest='target',
                           const='reverse',
                           help="Only pre-warm tables for reverse queries")

    @staticmethod
    def run(args):
        params = ['warm.php']
        if args.target == 'reverse':
            params.append('--reverse-only')
        if args.target == 'search':
            params.append('--search-only')
        return run_legacy_script(*params, nominatim_env=args)


class QueryExport:
    """\
    Export addresses as CSV file from the database.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Output arguments')
        group.add_argument('--output-type', default='street',
                           choices=('continent', 'country', 'state', 'county',
                                    'city', 'suburb', 'street', 'path'),
                           help='Type of places to output (default: street)')
        group.add_argument('--output-format',
                           default='street;suburb;city;county;state;country',
                           help="""Semicolon-separated list of address types
                                   (see --output-type). Multiple ranks can be
                                   merged into one column by simply using a
                                   comma-separated list.""")
        group.add_argument('--output-all-postcodes', action='store_true',
                           help="""List all postcodes for address instead of
                                   just the most likely one""")
        group.add_argument('--language',
                           help="""Preferred language for output
                                   (use local name, if omitted)""")
        group = parser.add_argument_group('Filter arguments')
        group.add_argument('--restrict-to-country', metavar='COUNTRY_CODE',
                           help='Export only objects within country')
        group.add_argument('--restrict-to-osm-node', metavar='ID', type=int,
                           help='Export only children of this OSM node')
        group.add_argument('--restrict-to-osm-way', metavar='ID', type=int,
                           help='Export only children of this OSM way')
        group.add_argument('--restrict-to-osm-relation', metavar='ID', type=int,
                           help='Export only children of this OSM relation')


    @staticmethod
    def run(args):
        params = ['export.php',
                  '--output-type', args.output_type,
                  '--output-format', args.output_format]
        if args.output_all_postcodes:
            params.append('--output-all-postcodes')
        if args.language:
            params.extend(('--language', args.language))
        if args.restrict_to_country:
            params.extend(('--restrict-to-country', args.restrict_to_country))
        if args.restrict_to_osm_node:
            params.extend(('--restrict-to-osm-node', args.restrict_to_osm_node))
        if args.restrict_to_osm_way:
            params.extend(('--restrict-to-osm-way', args.restrict_to_osm_way))
        if args.restrict_to_osm_relation:
            params.extend(('--restrict-to-osm-relation', args.restrict_to_osm_relation))

        return run_legacy_script(*params, nominatim_env=args)


class AdminServe:
    """\
    Start a simple web server for serving the API.

    This command starts the built-in PHP webserver to serve the website
    from the current project directory. This webserver is only suitable
    for testing and develop. Do not use it in production setups!

    By the default, the webserver can be accessed at: http://127.0.0.1:8088
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Server arguments')
        group.add_argument('--server', default='127.0.0.1:8088',
                           help='The address the server will listen to.')

    @staticmethod
    def run(args):
        run_php_server(args.server, args.project_dir / 'website')

STRUCTURED_QUERY = (
    ('street', 'housenumber and street'),
    ('city', 'city, town or village'),
    ('county', 'county'),
    ('state', 'state'),
    ('country', 'country'),
    ('postalcode', 'postcode')
)

EXTRADATA_PARAMS = (
    ('addressdetails', 'Include a breakdown of the address into elements.'),
    ('extratags', """Include additional information if available
                     (e.g. wikipedia link, opening hours)."""),
    ('namedetails', 'Include a list of alternative names.')
)

DETAILS_SWITCHES = (
    ('addressdetails', 'Include a breakdown of the address into elements.'),
    ('keywords', 'Include a list of name keywords and address keywords.'),
    ('linkedplaces', 'Include a details of places that are linked with this one.'),
    ('hierarchy', 'Include details of places lower in the address hierarchy.'),
    ('group_hierarchy', 'Group the places by type.'),
    ('polygon_geojson', 'Include geometry of result.')
)

def _add_api_output_arguments(parser):
    group = parser.add_argument_group('Output arguments')
    group.add_argument('--format', default='jsonv2',
                       choices=['xml', 'json', 'jsonv2', 'geojson', 'geocodejson'],
                       help='Format of result')
    for name, desc in EXTRADATA_PARAMS:
        group.add_argument('--' + name, action='store_true', help=desc)

    group.add_argument('--lang', '--accept-language', metavar='LANGS',
                       help='Preferred language order for presenting search results')
    group.add_argument('--polygon-output',
                       choices=['geojson', 'kml', 'svg', 'text'],
                       help='Output geometry of results as a GeoJSON, KML, SVG or WKT.')
    group.add_argument('--polygon-threshold', type=float, metavar='TOLERANCE',
                       help="""Simplify output geometry.
                               Parameter is difference tolerance in degrees.""")


class APISearch:
    """\
    Execute API search query.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Query arguments')
        group.add_argument('--query',
                           help='Free-form query string')
        for name, desc in STRUCTURED_QUERY:
            group.add_argument('--' + name, help='Structured query: ' + desc)

        _add_api_output_arguments(parser)

        group = parser.add_argument_group('Result limitation')
        group.add_argument('--countrycodes', metavar='CC,..',
                           help='Limit search results to one or more countries.')
        group.add_argument('--exclude_place_ids', metavar='ID,..',
                           help='List of search object to be excluded')
        group.add_argument('--limit', type=int,
                           help='Limit the number of returned results')
        group.add_argument('--viewbox', metavar='X1,Y1,X2,Y2',
                           help='Preferred area to find search results')
        group.add_argument('--bounded', action='store_true',
                           help='Strictly restrict results to viewbox area')

        group = parser.add_argument_group('Other arguments')
        group.add_argument('--no-dedupe', action='store_false', dest='dedupe',
                           help='Do not remove duplicates from the result list')


    @staticmethod
    def run(args):
        if args.query:
            params = dict(q=args.query)
        else:
            params = {k : getattr(args, k) for k, _ in STRUCTURED_QUERY if getattr(args, k)}

        for param, _ in EXTRADATA_PARAMS:
            if getattr(args, param):
                params[param] = '1'
        for param in ('format', 'countrycodes', 'exclude_place_ids', 'limit', 'viewbox'):
            if getattr(args, param):
                params[param] = getattr(args, param)
        if args.lang:
            params['accept-language'] = args.lang
        if args.polygon_output:
            params['polygon_' + args.polygon_output] = '1'
        if args.polygon_threshold:
            params['polygon_threshold'] = args.polygon_threshold
        if args.bounded:
            params['bounded'] = '1'
        if not args.dedupe:
            params['dedupe'] = '0'

        return run_api_script('search', args.project_dir,
                              phpcgi_bin=args.phpcgi_path, params=params)

class APIReverse:
    """\
    Execute API reverse query.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Query arguments')
        group.add_argument('--lat', type=float, required=True,
                           help='Latitude of coordinate to look up (in WGS84)')
        group.add_argument('--lon', type=float, required=True,
                           help='Longitude of coordinate to look up (in WGS84)')
        group.add_argument('--zoom', type=int,
                           help='Level of detail required for the address')

        _add_api_output_arguments(parser)


    @staticmethod
    def run(args):
        params = dict(lat=args.lat, lon=args.lon)
        if args.zoom is not None:
            params['zoom'] = args.zoom

        for param, _ in EXTRADATA_PARAMS:
            if getattr(args, param):
                params[param] = '1'
        if args.format:
            params['format'] = args.format
        if args.lang:
            params['accept-language'] = args.lang
        if args.polygon_output:
            params['polygon_' + args.polygon_output] = '1'
        if args.polygon_threshold:
            params['polygon_threshold'] = args.polygon_threshold

        return run_api_script('reverse', args.project_dir,
                              phpcgi_bin=args.phpcgi_path, params=params)


class APILookup:
    """\
    Execute API reverse query.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Query arguments')
        group.add_argument('--id', metavar='OSMID',
                           action='append', required=True, dest='ids',
                           help='OSM id to lookup in format <NRW><id> (may be repeated)')

        _add_api_output_arguments(parser)


    @staticmethod
    def run(args):
        params = dict(osm_ids=','.join(args.ids))

        for param, _ in EXTRADATA_PARAMS:
            if getattr(args, param):
                params[param] = '1'
        if args.format:
            params['format'] = args.format
        if args.lang:
            params['accept-language'] = args.lang
        if args.polygon_output:
            params['polygon_' + args.polygon_output] = '1'
        if args.polygon_threshold:
            params['polygon_threshold'] = args.polygon_threshold

        return run_api_script('lookup', args.project_dir,
                              phpcgi_bin=args.phpcgi_path, params=params)


class APIDetails:
    """\
    Execute API lookup query.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('Query arguments')
        objs = group.add_mutually_exclusive_group(required=True)
        objs.add_argument('--node', '-n', type=int,
                          help="Look up the OSM node with the given ID.")
        objs.add_argument('--way', '-w', type=int,
                          help="Look up the OSM way with the given ID.")
        objs.add_argument('--relation', '-r', type=int,
                          help="Look up the OSM relation with the given ID.")
        objs.add_argument('--place_id', '-p', type=int,
                          help='Database internal identifier of the OSM object to look up.')
        group.add_argument('--class', dest='object_class',
                           help="""Class type to disambiguated multiple entries
                                   of the same object.""")

        group = parser.add_argument_group('Output arguments')
        for name, desc in DETAILS_SWITCHES:
            group.add_argument('--' + name, action='store_true', help=desc)
        group.add_argument('--lang', '--accept-language', metavar='LANGS',
                           help='Preferred language order for presenting search results')

    @staticmethod
    def run(args):
        if args.node:
            params = dict(osmtype='N', osmid=args.node)
        elif args.way:
            params = dict(osmtype='W', osmid=args.node)
        elif args.relation:
            params = dict(osmtype='R', osmid=args.node)
        else:
            params = dict(place_id=args.place_id)
        if args.object_class:
            params['class'] = args.object_class
        for name, _ in DETAILS_SWITCHES:
            params[name] = '1' if getattr(args, name) else '0'

        return run_api_script('details', args.project_dir,
                              phpcgi_bin=args.phpcgi_path, params=params)


class APIStatus:
    """\
    Execute API status query.
    """

    @staticmethod
    def add_args(parser):
        group = parser.add_argument_group('API parameters')
        group.add_argument('--format', default='text', choices=['text', 'json'],
                           help='Format of result')

    @staticmethod
    def run(args):
        return run_api_script('status', args.project_dir,
                              phpcgi_bin=args.phpcgi_path,
                              params=dict(format=args.format))


def nominatim(**kwargs):
    """\
    Command-line tools for importing, updating, administrating and
    querying the Nominatim database.
    """
    parser = CommandlineParser('nominatim', nominatim.__doc__)

    parser.add_subcommand('import', SetupAll)
    parser.add_subcommand('freeze', SetupFreeze)
    parser.add_subcommand('replication', UpdateReplication)

    parser.add_subcommand('check-database', AdminCheckDatabase)
    parser.add_subcommand('warm', AdminWarm)

    parser.add_subcommand('special-phrases', SetupSpecialPhrases)

    parser.add_subcommand('add-data', UpdateAddData)
    parser.add_subcommand('index', UpdateIndex)
    parser.add_subcommand('refresh', UpdateRefresh)

    parser.add_subcommand('export', QueryExport)
    parser.add_subcommand('serve', AdminServe)

    if kwargs.get('phpcgi_path'):
        parser.add_subcommand('search', APISearch)
        parser.add_subcommand('reverse', APIReverse)
        parser.add_subcommand('lookup', APILookup)
        parser.add_subcommand('details', APIDetails)
        parser.add_subcommand('status', APIStatus)
    else:
        parser.parser.epilog = 'php-cgi not found. Query commands not available.'

    return parser.run(**kwargs)
