"""
Category pairlist filter, based on coingecko's "category" market filter.

Allows to define "include" and "exclude" filters, which lists of categories for each.
The "include" filter requires each coin to be part of all the specified categories.
The "exclude" filter requires each coin to be not part of any of the specified categories.

Category lookup is done via coingecko's public API, and lists are cached for
`refresh_period` seconds (default 86400). When cache updates fails due to network errors,
`ignore_failures` decides whether to allow the pairs (true/default), or raise an exception
(false).

The `vs_currency` config parameter is required by the coingecko API in order to retrieve
market data. To determine category membership, it should be irrelevant. It is set to
"USD" by default.

Example config:
```json
"pairlists": [
    {
        "method": "CategoryFilter",
        "include": [
            "meme-token"
        ],
        "exclude": [
            "stablecoins",
            "governance",
            "fan-token"
        ],
        "ignore_failures": false,
        "refresh_period": 86400,
        "vs_currency": "USD"
    }
```

Example list of categories:
```sh
curl -X GET "https://api.coingecko.com/api/v3/coins/categories/list" -H  "accept: application/json"
```

"""
from copy import deepcopy
import logging
import time
from typing import Any, Dict, List

from cachetools.ttl import TTLCache
from pycoingecko import CoinGeckoAPI

from freqtrade.exceptions import OperationalException, TemporaryError
from freqtrade.plugins.pairlist.IPairList import IPairList


logger = logging.getLogger(__name__)


class CategoryFilter(IPairList):
    '''
    Filters pairs by category membership or non-membership.
    '''

    def __init__(self, exchange, pairlistmanager,
                 config: Dict[str, Any], pairlistconfig: Dict[str, Any],
                 pairlist_pos: int) -> None:
        super().__init__(exchange, pairlistmanager, config, pairlistconfig, pairlist_pos)

        self._coingecko = CoinGeckoAPI()
        self._coingecko_limit = 0.1
        self._stake_currency = config['stake_currency']
        self._filters: Dict[str, Dict[str, Any]] = {
            'include': {
                'categories': pairlistconfig.get('include', []),
                'rule': lambda coin, coin_list: coin in coin_list,
            },
            'exclude': {
                'categories': pairlistconfig.get('exclude', []),
                'rule': lambda coin, coin_list: coin not in coin_list,
            }
        }
        self._ignore_failures = pairlistconfig.get('ignore_failures', True)
        self._refresh_period = pairlistconfig.get('refresh_period', 86400)
        self._vs_currency = pairlistconfig.get('vs_currency', 'USD').lower()

        for filter in self._filters:
            if not isinstance(self._filters[filter]['categories'], list):
                raise OperationalException(f"CategoryFilter: '{filter}' must be a list of strings")

        # Cache will manage only 1 item: Dict[str, Dict[str, List[str]]]
        self._filters_cache: TTLCache = TTLCache(maxsize=1, ttl=self._refresh_period)

    @property
    def needstickers(self) -> bool:
        """
        Overrides IPairList::needstickers().
        No tickers are needed for this filter.
        """
        return True

    def short_desc(self) -> str:
        """
        Short whitelist method description - used for startup-messages
        """
        return (f"{self.name} - Filtering pairs by coingeckos categories (include/exclude)")

    def gen_pairlist(self, tickers: Dict) -> List[str]:

        logger.error('GEN')

        try:
            filter_lists = self._cached_filter_lists()
        except TemporaryError as exc:
            if self._ignore_failures:
                logger.warning("Failed to fetch coingecko filter lists. Returning empty pairlist '%s', "
                               "because 'ignore_failures=true'", pair)
                logger.warning("Exception was: %s", str(exc))
                return []
            raise exc

        pairs = []
        filtered_tickers = [
                v for k, v in tickers.items()
                if self._exchange.get_pair_quote_currency(k) == self._stake_currency]
        pairlist = [s['symbol'] for s in filtered_tickers]

        return self.filter_pairlist(pairlist, tickers)


    def filter_pairlist(self, pairlist: List[str], tickers: Dict) -> List[str]:
        """
        Overrides IPairList::filter_pairlist().

        Filter the pairlist based on ::_filter_pair().

        :param pairlist: The incoming pairlist to be filtered
        :param tickers: Expect to be `None` (not needed)
        :return: The filtered pairlist
        """
        if not self._enabled:
            return pairlist

        try:
            filter_lists = self._cached_filter_lists()
        except TemporaryError as exc:
            if self._ignore_failures:
                logger.warning("Failed to fetch coingecko filter lists. Accepting pair '%s', "
                               "because 'ignore_failures=true'", pair)
                logger.warning("Exception was: %s", str(exc))
                return pairlist
            raise exc

        # Copy list since we're modifying this list
        orig_pairlist = deepcopy(pairlist)
        for pair in orig_pairlist:
            if self._filter_pair(pair, filter_lists):
                pairlist.remove(pair)
        self.log_once(f"Validated {len(pairlist)} pairs, "
                      f"filtered {len(orig_pairlist) - len(pairlist)} pairs", logger.info)
        return pairlist

    def _filter_pair(self, pair: str, filter_lists: Dict[str, Dict[str, List[str]]]) -> bool:
        """
        Filter a pair, based on given `filter_lists`, containing include/exclude coin-lists for each category,
        and based on our `self` filter settings.

        A pair is filtered, if it's base pair is not present in any of the 'include' category lists, or if it is
        present in any of the 'exclude' category lists.

        :param pair: The pair to filter or not to filter
        :param filter_lists: The categorized coin lists to filter against
        :return: `True` if the pair is filtered out, `False` otherwise
        """
        base_curr = self._exchange.get_pair_base_currency(pair)
        for filter in self._filters:
            for category in self._filters[filter]['categories']:
                if not self._filters[filter]['rule'](base_curr, filter_lists[filter][category]):
                    logger.info(f"Ignoring {pair} because '{base_curr}' is '{filter}' filtered "
                                f"for category '{category}'")
                    return True
        return False


    def _cached_filter_lists(self) -> Dict[str, Dict[str, List[str]]]:
        """
        Retrieve the cached filter lists, if possible, otherwise fetch it fresh from coingecko.

        :return: The cached or fresh filter lists
        :raises: TemporaryError on cache update errors
        """
        filter_lists = self._filters_cache.get('single_item', None)
        if not filter_lists:
            filter_lists = self._fetch_filter_lists()
            self._filters_cache['single_item'] = filter_lists
        return filter_lists

    def _fetch_filter_lists(self) -> Dict[str, Dict[str, List[str]]]:
        """
        Fetch all coins for all categories from coingecko, for which we have include/exclude
        filters.

        :return: A dict of coin lists for each filter (include/exclude) for each category as a
                 lookup-table
        :raises: TemporaryError on coingecko API trouble
        """
        filter_lists: Dict[str, Dict[str, List[str]]] = {}
        for filter in self._filters:
            filter_lists[filter] = {}
            for category in self._filters[filter]['categories']:
                try:
                    markets = self._coingecko.get_coins_markets(self._vs_currency,
                                                                category=category)
                except Exception as exc:
                    raise TemporaryError(f'Failed to fetch from coingecko: {str(exc)}')
                filter_lists[filter][category] = [coin['symbol'].upper() for coin in markets]
                logger.info(f"Loaded coins for category '{category}' ('{filter}' filter): "
                            f"{filter_lists[filter][category]}")
                time.sleep(self._coingecko_limit)
        return filter_lists
