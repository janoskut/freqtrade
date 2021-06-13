"""
Category pairlist filter, based on coingecko's "category" market filter.

Allows to define "include" and "exclude" filters, which lists of categories for each.
The "include" filter requires each coin to be part of all the specified categories.
The "exclude" filter requires each coin to be not part of any of the specified categories.

Category lookup is done via coingecko's public API, and lists are cached for
`refresh_period` seconds (default 86400). When cache updates fails due to network errors,
`ignore_failures` decides whether to allow the pairs (true/default), or raise an exception
(false).

Example config:
```json
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
        "vs_currency": "usd"
    }
```

Example list of categories:
```sh
curl -X GET "https://api.coingecko.com/api/v3/coins/categories/list" -H  "accept: application/json"
```

"""
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
        self._vs_currency = pairlistconfig.get('vs_currency', 'usd')

        for filter in self._filters:
            if not isinstance(self._filters[filter]['categories'], list):
                raise OperationalException(f"CategoryFilter: '{filter}' must be a list of strings")

        # Cache will manage only 1 item: Dict[str, Dict[str, List[str]]]
        self._filters_cache: TTLCache = TTLCache(maxsize=1, ttl=_refresh_period)

    @property
    def needstickers(self) -> bool:
        """
        Overrides IPairList::needstickers().
        No tickers are needed for this filter.
        """
        return False

    def short_desc(self) -> str:
        """
        Short whitelist method description - used for startup-messages
        """
        return (f"{self.name} - Filtering pairs by coingeckos categories (include/exclude)")

    def _validate_pair(self, pair: str, ticker: Dict[str, Any]) -> bool:
        """
        Overrides IPairList::_validate_pair().

        Refreshes category filter lists cache from coingecko, when necessary.
        Accepts coins if cache update fails and ignore_failures=true, otherwise raises TemporaryError.

        :raises: TemporaryError when cache update fails
        """
        source_coin = pair.split('/')[0]

        try:
            filter_lists = self._cached_filter_lists()
        except TemporaryError as exc:
            if self._ignore_failures:
                logger.warning("Failed to fetch coingecko filter lists. Accepting pair '%s', "
                               "because 'ignore_failures=true'", pair)
                logger.warning("Exception was: %s", str(exc))
                return True
            raise exc

        for filter in self._filters:
            for category in self._filters[filter]['categories']:
                if not self._filters[filter]['rule'](source_coin, filter_lists[filter][category]):
                    logger.info(f"Ignoring {pair} because '{source_coin}' is '{filter}' filtered "
                                f"for category '{category}'")
                    return False
        return True

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
        Fetch all coins for all categories from coingecko, for which we have include/exclude filters.

        :return: A dict of coin lists for each filter (include/exclude) for each category as a lookup-table
        :raises: TemporaryError on coingecko API trouble
        """
        filter_lists: Dict[str, Dict[str, List[str]]] = {}
        for filter in self._filters:
            filter_lists[filter] = {}
            for category in self._filters[filter]['categories']:
                try:
                    markets = self._coingecko.get_coins_markets(self._vs_currency, category=category)
                except Exception as exc:
                    raise TemporaryError(f'Failed to fetch from coingecko: {str(exc)}')
                filter_lists[filter][category] = [coin['symbol'].upper() for coin in markets]
                logger.info(f"Loaded coins for category '{category}' ('{filter}' filter): "
                            f"{filter_lists[filter][category]}")
                time.sleep(self._coingecko_limit)
        return filter_lists
