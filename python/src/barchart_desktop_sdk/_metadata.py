"""Each message's symbol names and market definition, which the Openfeed client doesn't give. One per connection, used
only on its socket thread.
"""

from __future__ import annotations

from openfeed.generated import openfeed_api_pb2, openfeed_instrument_pb2, openfeed_pb2

from ._js import parse_int
from ._types import MessageMetadata

_Definition = openfeed_instrument_pb2.InstrumentDefinition
_SymbolSubscription = tuple[str, int]
_Known = tuple[_Definition | None, list[_SymbolSubscription]]


def _get_next_alias(old_alias: str) -> str:
    """The alias a rolling instrument takes next: `ZC*1` becomes `ZC*2`, and `ZC*0` stays."""
    parts = old_alias.split("*")
    if old_alias.endswith("*0"):
        return f"{parts[0]}*0"
    number = parse_int(parts[1]) if len(parts) > 1 else None
    return old_alias if number is None else f"{parts[0]}*{number + 1}"


class MetadataTracker:
    def __init__(self) -> None:
        self._known: dict[int, _Known] = {}

    def get_metadata(self, message: openfeed_api_pb2.OpenfeedGatewayMessage) -> MessageMetadata:
        """The message's metadata, as things stood before it, plus the symbol a subscription response adds."""
        definition, symbols = self._apply(message)
        return MessageMetadata(symbol_names=tuple(dict.fromkeys(symbol for symbol, _ in symbols)), definition=definition)

    def _get(self, market_id: int) -> _Known:
        return self._known.get(market_id, (None, []))

    def _apply(self, message: openfeed_api_pb2.OpenfeedGatewayMessage) -> _Known:  # noqa: PLR0911
        match message.WhichOneof("data"):
            case "subscriptionResponse":
                return self._apply_response(message.subscriptionResponse)
            case "instrumentDefinition":
                market_id = message.instrumentDefinition.marketId
                known = self._get(market_id)
                self._known[market_id] = (message.instrumentDefinition, known[1])
                return known
            case "instrumentAction":
                return self._apply_action(message.instrumentAction)
            case "marketSnapshot":
                return self._get(message.marketSnapshot.marketId)
            case "marketUpdate":
                return self._get(message.marketUpdate.marketId)
            case "ohlc":
                return self._get(message.ohlc.marketId)
            case "volumeAtPrice":
                return self._get(message.volumeAtPrice.marketId)
            case _:
                return None, []

    def _apply_response(self, response: openfeed_api_pb2.SubscriptionResponse) -> _Known:
        definition, symbols = self._get(response.marketId)
        if response.status.result != openfeed_api_pb2.SUCCESS:
            return definition, symbols

        current = (response.symbol, response.correlationId)
        if response.unsubscribe:
            kept = [symbol for symbol in symbols if symbol != current]
            self._set_or_forget(response.marketId, definition, kept)
            return definition, symbols

        symbols = symbols if current in symbols else [*symbols, current]
        self._known[response.marketId] = (definition, symbols)
        return definition, symbols

    def _apply_action(self, action: openfeed_pb2.InstrumentAction) -> _Known:
        if not action.HasField("instrument"):
            return None, []
        market_id = action.instrument.marketId
        known = self._get(market_id)
        definition, symbols = known

        if action.action == openfeed_pb2.ALIAS_CHANGED:
            self._set_or_forget(market_id, definition, [symbol for symbol in symbols if symbol[0] != action.oldAlias])
            if action.HasField("newInstrument"):
                new_id = action.newInstrument.marketId
                new_definition, new_symbols = self._get(new_id)
                new_alias = _get_next_alias(action.oldAlias)
                kept = new_symbols if new_alias == action.oldAlias else [symbol for symbol in new_symbols if symbol[0] != new_alias]
                self._set_or_forget(new_id, new_definition, kept)
        elif action.action == openfeed_pb2.EXCHANGE_MOVE:
            self._known.pop(market_id, None)
            if action.HasField("newInstrument"):
                self._known[action.newInstrument.marketId] = known
        return known

    def _set_or_forget(self, market_id: int, definition: _Definition | None, symbols: list[_SymbolSubscription]) -> None:
        if symbols:
            self._known[market_id] = (definition, symbols)
        else:
            self._known.pop(market_id, None)
