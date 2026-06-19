#!/usr/bin/python

from scholarly import scholarly

# from scholarly import ProxyGenerator
# # Set up a ProxyGenerator object to use free proxies
# # This needs to be done only once per session
# pg = ProxyGenerator()
# pg.FreeProxies()
# scholarly.use_proxy(pg)
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
AUTHOR = config.AUTHOR_NAME


def main(author=AUTHOR):
    """Ignores the per publication citations, and only updates the total line."""
    total_line, pubs = get_all_citations(author)
    return total_line, pubs


def get_all_citations(author):
    # Retrieve the author's data, fill-in, and print
    # Get an iterator for the author results
    search_query = scholarly.search_author(author)
    # Retrieve the first result from the iterator
    first_author_result = next(search_query)
    scholarly.pprint(first_author_result)

    # Retrieve all the details for the author
    author = scholarly.fill(first_author_result)
#    scholarly.pprint(author)

    pubs = {}

    total = author['citedby']
    h_index = author['hindex']
    i10_index = author['i10index']

    total_line = 'Total Citations: {}, h-index: {}, i10-index: {} (by Google Scholar).'.format(
        total, h_index, i10_index)

    for p in author['publications']:
        # print(p)
        id = p['author_pub_id']
        n = p['num_citations']
#        print(title, "\\\\gsurl{"+id+"}")
        pubs[id] = str(n)

    return total_line, pubs


if __name__ == '__main__':
    main()
