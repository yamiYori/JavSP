"""Read-only JavLibrary metadata probe: never scans or renames media."""
import argparse
import json
import logging


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ids', nargs='+', help='A small number of DVD IDs to validate')
    parser.add_argument('-c', '--config', help='JavSP YAML configuration')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    from javsp.datatype import MovieInfo
    from javsp.web import javlib
    from javsp.web.exceptions import CrawlerError
    rows = []
    try:
        for avid in args.ids:
            movie = MovieInfo(avid)
            try:
                javlib.parse_data(movie)
                rows.append({'requested_id': avid, 'dvdid': movie.dvdid,
                             'url': movie.url, 'title_present': bool(movie.title),
                             'cover_present': bool(movie.cover),
                             'actress_count': len(movie.actress or []),
                             'route': javlib.request.selected, 'ok': True})
            except CrawlerError as exc:
                rows.append({'requested_id': avid, 'ok': False, 'error': str(exc)})
    finally:
        javlib.request.close()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0 if all(row['ok'] for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
