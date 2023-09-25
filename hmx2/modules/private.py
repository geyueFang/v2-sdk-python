from web3 import Web3, Account
from web3.middleware.signing import construct_sign_and_send_raw_middleware
from web3.logs import DISCARD
from hmx2.constants import (
  TOKEN_PROFILE,
  COLLATERAL_WETH,
    MULTICALL_ADDRESS,
    CROSS_MARGIN_HANDLER_ADDRESS,
    LIMIT_TRADE_HANDLER_ADDRESS,
    VAULT_STORAGE_ADDRESS,
    PERP_STORAGE_ADDRESS,
    CONFIG_STORAGE_ADDRESS,
    CROSS_MARGIN_HANDLER_ABI_PATH,
    LIMIT_TRADE_HANDLER_ABI_PATH,
    VAULT_STORAGE_ABI_PATH,
    PERP_STORAGE_ABI_PATH,
    CONFIG_STORAGE_ABI_PATH,
    ERC20_ABI_PATH,
    COLLATERALS,
    COLLATERAL_ASSET_ID_MAP,
    ADDRESS_ZERO,
    MAX_UINT,
    EXECUTION_FEE,
    BPS,
)
from hmx2.enum import Cmd
from hmx2.helpers.contract_loader import load_contract
from simple_multicall import Multicall
from hmx2.modules.oracle.oracle_middleware import OracleMiddleware
from eth_abi.abi import encode, decode
from hmx2.modules.calculator.fee import FeeCalculator
import decimal

decimal.getcontext().prec = 15
decimal.getcontext().rounding = "ROUND_HALF_DOWN"


class Private(object):

  def __init__(self, chain_id: int, eth_provider: Web3,
               eth_signer: Account, oracle_middleware: OracleMiddleware):
    self.chain_id = chain_id
    self.eth_provider = eth_provider
    self.eth_signer = eth_signer
    self.oracle_middleware = oracle_middleware
    self.eth_provider.middleware_onion.add(
      construct_sign_and_send_raw_middleware(self.eth_signer))
    # load contract
    self.limit_trade_handler_instance = load_contract(
      self.eth_provider, LIMIT_TRADE_HANDLER_ADDRESS, LIMIT_TRADE_HANDLER_ABI_PATH)
    self.perp_storage_instance = load_contract(
      self.eth_provider, PERP_STORAGE_ADDRESS, PERP_STORAGE_ABI_PATH)
    self.config_storage_instance = load_contract(
      self.eth_provider, CONFIG_STORAGE_ADDRESS, CONFIG_STORAGE_ABI_PATH)
    self.vault_storage_instance = load_contract(
      self.eth_provider, VAULT_STORAGE_ADDRESS, VAULT_STORAGE_ABI_PATH)
    self.multicall_instance = Multicall(w3=self.eth_provider,
                                        chain='arbitrum', custom_address=MULTICALL_ADDRESS)

  def get_public_address(self):
    '''
    Get the public address of the signer.
    '''
    return self.eth_signer.address

  def get_collaterals(self, sub_account_id: int):
    self.__check_sub_account_id_param(sub_account_id)

    calls = [
      self.multicall_instance.create_call(
        self.vault_storage_instance,
          "traderBalances",
          [self.eth_signer.address, collateral],
      )
      for collateral in COLLATERALS
    ]
    collateral_usd = [
      self.oracle_middleware.get_price(COLLATERAL_ASSET_ID_MAP[collateral])
      for collateral in COLLATERALS
    ]

    results = self.multicall_instance.call(calls)

    ret = {}
    for index, collateral in enumerate(COLLATERALS):
      amount = int(
          results[1][index].hex(), 16) / 10 ** TOKEN_PROFILE[collateral]["decimals"]
      ret[TOKEN_PROFILE[collateral]["symbol"]] = {
        'amount': amount,
        'value_usd': amount * collateral_usd[index]
      }

    return ret

  def deposit_erc20_collateral(self, sub_account_id: int, token_address: str, amount: float):
    '''
    Deposit ERC20 token as collateral.

    :param sub_account_id: required
    :type sub_account_id: int between 0 and 255

    :param token_address: required
    :type token_address: str in list COLLATERALS

    :param amount: required
    :type amount: float
    '''
    if token_address not in COLLATERALS:
      raise Exception("Invalid collateral address")
    self.__check_sub_account_id_param(sub_account_id)

    amount_wei = amount * 10 ** TOKEN_PROFILE[token_address]["decimals"]
    token_instance = load_contract(
      self.eth_provider, token_address, ERC20_ABI_PATH)

    allowance = token_instance.functions.allowance(
        self.eth_signer.address, CROSS_MARGIN_HANDLER_ADDRESS).call()
    if allowance < amount_wei:
      token_instance.functions.approve(
          CROSS_MARGIN_HANDLER_ADDRESS, amount_wei).transact({"from": self.eth_signer.address})

    cross_margin_handler_instance = load_contract(
      self.eth_provider, CROSS_MARGIN_HANDLER_ADDRESS, CROSS_MARGIN_HANDLER_ABI_PATH)
    return cross_margin_handler_instance.functions.depositCollateral(
      sub_account_id,
      token_address,
      amount_wei,
      False
    ).transact({"from": self.eth_signer.address})

  def deposit_eth_collateral(self, sub_account_id: int, amount: float):
    '''
    Deposit ETH as collateral.

    :param sub_account_id: required
    :type sub_account_id: int between 0 and 255

    :param amount: required
    :type amount: float
    '''
    self.__check_sub_account_id_param(sub_account_id)

    amount_wei = amount * 10 ** 18

    cross_margin_handler_instance = load_contract(
      self.eth_provider, CROSS_MARGIN_HANDLER_ADDRESS, CROSS_MARGIN_HANDLER_ABI_PATH)
    return cross_margin_handler_instance.functions.depositCollateral(
      sub_account_id,
      COLLATERAL_WETH,
      amount_wei,
      True
    ).transact({"from": self.eth_signer.address, "value": amount_wei})

  def create_market_order(self, sub_account_id: int, market_index: int, buy: bool, size: float, reduce_only: bool, tp_token: str = ADDRESS_ZERO):
    '''
    Post a market order

    :param sub_account_id: requied
    :type sub_account_id: int between 0 and 255

    :param market_index: requied
    :type market_index: int in list MARKET

    :param buy: requied
    :type buy: bool

    :param size: required
    :type size: float

    :param reduce_only: requied
    :type reduce_only: bool

    :param tp_token
    :type tp_token: str in list COLLATERALS address
    '''
    self.__check_sub_account_id_param(sub_account_id)

    order = {
      "cmd": Cmd.CREATE,
      "market_index": market_index,
      "size_delta": Web3.to_wei(size, "tether") if buy else -Web3.to_wei(size, "tether"),
      "trigger_price": 0,
      "acceptable_price": MAX_UINT if buy else 0,
      "trigger_above_threshold": True,
      "execution_fee": EXECUTION_FEE,
      "reduce_only": reduce_only,
      "tp_token": tp_token
    }

    tx = self.__create_order_batch(
      self.eth_signer.address, sub_account_id, [order]
    )
    events = self.__parse_log(tx, "LogCreateLimitOrder")
    args = {}
    args["tx"] = events[0]["transactionHash"]
    args["order"] = events[0]["args"]
    return args

  def create_trigger_order(self, sub_account_id: int, market_index: int, buy: bool, size: float, trigger_price: float, trigger_above_threshold: bool, reduce_only: bool, tp_token: str = ADDRESS_ZERO):
    '''
    Post a trigger order

    :param sub_account_id: requied
    :type sub_account_id: int between 0 and 255

    :param market_index: requied
    :type market_index: int in list MARKET

    :param buy: requied
    :type buy: bool

    :param size: required
    :type size: float

    :param trigger_price: required
    :type trigger_price: float

    :param trigger_above_threshold: requied
    :type trigger_above_threshold: bool

    :param reduce_only: requied
    :type reduce_only: bool

    :param tp_token
    :type tp_token: str in list COLLATERALS address
    '''
    order = {
      "cmd": Cmd.CREATE,
      "market_index": market_index,
      "size_delta": Web3.to_wei(size, "tether") if buy else -Web3.to_wei(size, "tether"),
      "trigger_price": Web3.to_wei(trigger_price, "tether"),
      "acceptable_price": Web3.to_wei(
        self.__add_slippage(
          trigger_price) if size > 0 else self.__sub_slippage(trigger_price),
        "tether"
      ),
      "trigger_above_threshold": trigger_above_threshold,
      "execution_fee": EXECUTION_FEE,
      "reduce_only": reduce_only,
      "tp_token": tp_token
    }

    tx = self.__create_order_batch(
      self.eth_signer.address, sub_account_id, [order]
    )
    events = self.__parse_log(tx, "LogCreateLimitOrder")
    args = {}
    args["tx"] = events[0]["transactionHash"]
    args["order"] = events[0]["args"]
    return args

  def update_trigger_order(self, sub_account_id: int, order_index: int, buy: bool, size: float, trigger_price: float, trigger_above_threshold: bool, reduce_only: bool, tp_token: str):
    '''
    Update a trigger order

    :param sub_account_id: requied
    :type sub_account_id: int between 0 and 255

    :param order_index: requied
    :type order_index: int

    :param buy: requied
    :type buy: bool

    :param size: required
    :type size: float

    :param trigger_price: required
    :type trigger_price: float

    :param trigger_above_threshold: requied
    :type trigger_above_threshold: bool

    :param reduce_only: requied
    :type reduce_only: bool

    :param tp_token: requied
    :type tp_token: str in list COLLATERALS address
    '''
    size_delta = Web3.to_wei(size, "tether")
    acceptable_price = self.__add_slippage(
      trigger_price) if size > 0 else self.__sub_slippage(trigger_price)
    order = {
      "cmd": Cmd.UPDATE,
      "order_index": order_index,
      "size_delta": size_delta if buy else -size_delta,
      "trigger_price": Web3.to_wei(trigger_price, "tether"),
      "acceptable_price": Web3.to_wei(acceptable_price, "tether"),
      "trigger_above_threshold": trigger_above_threshold,
      "reduce_only": reduce_only,
      "tp_token": tp_token
    }

    tx = self.__create_order_batch(
      self.eth_signer.address, sub_account_id, [order]
    )
    events = self.__parse_log(tx, "LogUpdateLimitOrder")
    args = {}
    args["tx"] = events[0]["transactionHash"]
    args["order"] = events[0]["args"]
    return args

  def cancel_trigger_order(self, sub_account_id: int, order_index: int):
    '''
    Cancel a trigger order

    :param sub_account_id: requied
    :type sub_account_id: int between 0 and 255

    :param order_index: requied
    :type order_index: int
    '''
    order = {
      "cmd": Cmd.CANCEL,
      "order_index": order_index,
    }

    tx = self.__create_order_batch(
      self.eth_signer.address, sub_account_id, [order]
    )
    events = self.__parse_log(tx, "LogCancelLimitOrder")
    args = {}
    args["tx"] = events[0]["transactionHash"]
    args["order"] = events[0]["args"]
    return args

  def __add_slippage(self, value: float):
    return decimal.Decimal(value) * (BPS + 25) / BPS

  def __sub_slippage(self, value: float):
    return decimal.Decimal(value) * (BPS - 25) / BPS

  def __create_order_batch(self, account: str, sub_account_id: int, orders):
    (cmds, datas) = self.__prepare_batch_input(orders)
    return self.limit_trade_handler_instance.functions.batch(
      account,
      sub_account_id,
      cmds,
      datas
    ).transact({"from": self.eth_signer.address, "value": EXECUTION_FEE * cmds.count(Cmd.CREATE)})

  def __prepare_batch_input(self, orders):
    return ([order["cmd"] for order in orders], [self.__prepare_order_call_data(order) for order in orders])

  def __prepare_order_call_data(self, order):
    if order["cmd"] == Cmd.CREATE:
      return self.__prepare_create_order_call_data(
        order["market_index"],
        order["size_delta"],
        order["trigger_price"],
        order["acceptable_price"],
        order["trigger_above_threshold"],
        order["execution_fee"],
        order["reduce_only"],
        order["tp_token"]
      )
    if order["cmd"] == Cmd.UPDATE:
      return self.__prepare_update_order_call_data(
        order["order_index"],
        order["size_delta"],
        order["trigger_price"],
        order["acceptable_price"],
        order["trigger_above_threshold"],
        order["reduce_only"],
        order["tp_token"]
      )
    if order["cmd"] == Cmd.CANCEL:
      return self.__prepare_cancel_order_call_data(order["order_index"])

  def __prepare_create_order_call_data(self, market_index: int, size_delta: float, trigger_price: float, acceptable_price: float, trigger_above_threshold: bool, execution_fee: float, reduce_only: bool, tp_token: str):
    return encode(
      ["uint256", "int256", "uint256", "uint256",
       "bool", "uint256", "bool", "address"],
      [market_index, size_delta, trigger_price, acceptable_price,
       trigger_above_threshold, execution_fee, reduce_only, tp_token]
    )

  def __prepare_update_order_call_data(self, order_index: int, size_delta: float, trigger_price: float, acceptable_price: float, trigger_above_threshold: bool, reduce_only: bool, tp_token: str):
    return encode(
      ["uint256", "int256", "uint256", "uint256",
       "bool", "bool", "address"],
      [order_index, size_delta, trigger_price, acceptable_price,
       trigger_above_threshold, reduce_only, tp_token]
    )

  def __prepare_cancel_order_call_data(self, order_index: int):
    return encode(
      ["uint256"],
      [order_index]
    )

  def __check_sub_account_id_param(self, sub_account_id):
    if sub_account_id not in range(0, 256):
      raise Exception("Invalid sub account id")

  def __parse_log(self, tx, topic):
    receipt = self.eth_provider.eth.get_transaction_receipt(tx)
    return self.limit_trade_handler_instance.events[topic](
      ).process_receipt(receipt, DISCARD)

  def get_market(self, market_index: int):
    (
      long_position_size,
      long_accum_se,
      long_accum_s2e,
      short_position_size,
      short_accum_se,
      short_accum_s2e,
      current_funding_rate,
      last_funding_time,
      accum_funding_long,
      accum_funding_short,
      funding_accrued
    ) = self.perp_storage_instance.functions.markets(market_index).call()
    return {
      "long_position_size": long_position_size,
      "long_accum_se": long_accum_se,
      "long_accum_s2e": long_accum_s2e,
      "short_position_size": short_position_size,
      "short_accum_se": short_accum_se,
      "short_accum_s2e": short_accum_s2e,
      "current_funding_rate": current_funding_rate,
      "last_funding_time": last_funding_time,
      "accum_funding_long": accum_funding_long,
      "accum_funding_short": accum_funding_short,
      "funding_accrued": funding_accrued,
    }

  def get_trading_config(self):
    (
      funding_interval,
      min_profit_duration,
      dev_fee_rate_bps,
      max_position
    ) = self.config_storage_instance.functions.tradingConfig().call()
    return {
       "funding_interval": funding_interval,
        "min_profit_duration": min_profit_duration,
        "dev_fee_rate_bps": dev_fee_rate_bps,
        "max_position": max_position,
    }

  def get_market_config(self, market_index: int):
    (
      asset_id,
      max_long_position_size,
      max_short_position_size,
      increase_position_fee_rate_bps,
      decrease_position_fee_rate_bps,
      initial_margin_fraction_bps,
      maintenance_margin_fraction_bps,
      max_profit_rate_bps,
      asset_class,
      allow_increase_position,
      active,
      (max_skew_scale_usd, max_funding_rate)
    ) = self.config_storage_instance.functions.marketConfigs(market_index).call()
    return {
      "asset_id": asset_id.hex(),
      "max_long_position_size": max_long_position_size,
      "max_short_position_size": max_short_position_size,
      "increase_position_fee_rate_bps": increase_position_fee_rate_bps,
      "decrease_position_fee_rate_bps": decrease_position_fee_rate_bps,
      "initial_margin_fraction_bps": initial_margin_fraction_bps,
      "maintenance_margin_fraction_bps": maintenance_margin_fraction_bps,
      "max_profit_rate_bps": max_profit_rate_bps,
      "asset_class": asset_class,
      "allow_increase_position": allow_increase_position,
      "active": active,
      "max_skew_scale_usd": max_skew_scale_usd,
      "max_funding_rate": max_funding_rate
    }

  def get_market_info(self, market_index: int):
    '''
    Get a market info

    :param market_index: requied
    :type market_index: int in list Market
    '''
    data = self.multicall_market_data(market_index)
    block = self.get_block()
    tvl = self.get_hlp_tvl()

    funding_rate = FeeCalculator.get_funding_rate(
      data["trading_config"],
      data["market_config"],
      data["market"],
      block["timestamp"],
    )

    borrowing_rate = FeeCalculator.get_borrowing_rate(
      data["asset_class_config"], data["asset_class"], tvl)

    return {
      "market": data["market"],
      "asset_class": data["asset_class"],
      "market_config": data["market_config"],
      "asset_class_config": data["asset_class_config"],
      "trading_config": data["trading_config"],
      "funding_rate": funding_rate,
      "borrowing_rate": borrowing_rate
    }

  def get_block(self):
    return self.eth_provider.eth.get_block("latest")

  def multicall_market_data(self, market_index: int):
    calls = [
      self.multicall_instance.create_call(
        self.config_storage_instance, "marketConfigs", [market_index]
      ),
      self.multicall_instance.create_call(
        self.perp_storage_instance, "markets", [market_index]
      ),
      self.multicall_instance.create_call(
        self.config_storage_instance, "tradingConfig", []
      )
    ]

    results = self.multicall_instance.call(calls)
    data = results[1]
    (
      asset_id,
      max_long_position_size,
      max_short_position_size,
      increase_position_fee_rate_bps,
      decrease_position_fee_rate_bps,
      initial_margin_fraction_bps,
      maintenance_margin_fraction_bps,
      max_profit_rate_bps,
      asset_class,
      allow_increase_position,
      active,
      max_skew_scale_usd,
      max_funding_rate,
    ) = decode(
        ['bytes32', 'uint256', 'uint256', 'uint32', 'uint32', 'uint32',
         'uint32', 'uint32', 'uint8', 'bool', 'bool', 'uint256', 'uint256'],
        data.pop(0)
      )
    (
      long_position_size,
      long_accum_se,
      long_accum_s2e,
      short_position_size,
      short_accum_se,
      short_accum_s2e,
      current_funding_rate,
      last_funding_time,
      accum_funding_long,
      accum_funding_short,
      funding_accrued,
    ) = decode(
      ['uint256', 'uint256', 'uint256', 'uint256', 'uint256',
       'uint256', 'int256', 'uint256', 'int256', 'int256', 'int256'],
      data.pop(0)
    )
    (
      funding_interval,
      min_profit_duration,
      dev_fee_rate_bps,
      max_position
    ) = decode(
      ['uint256', 'uint256', 'uint32', 'uint8'],
      data.pop(0)
    )

    calls = [
      self.multicall_instance.create_call(
        self.config_storage_instance, "assetClassConfigs", [asset_class]
      ),
      self.multicall_instance.create_call(
        self.perp_storage_instance, "assetClasses", [asset_class]
      ),
    ]
    results = self.multicall_instance.call(calls)
    data = results[1]
    base_borrowing_rate = int(data.pop(0).hex(), 16)
    (
      reserve_value_e30,
      sum_borrowing_rate,
      last_borrowing_time,
      sum_borrowing_fee_e30,
      sum_settled_borrowing_fee_e30
    ) = decode(
      ['uint256', 'uint256', 'uint256', 'uint256', 'uint256'],
      data.pop(0)
    )

    return {
      "market_config": {
        "asset_id": asset_id.hex(),
        "max_long_position_size": max_long_position_size,
        "max_short_position_size": max_short_position_size,
        "increase_position_fee_rate_bps": increase_position_fee_rate_bps,
        "decrease_position_fee_rate_bps": decrease_position_fee_rate_bps,
        "initial_margin_fraction_bps": initial_margin_fraction_bps,
        "maintenance_margin_fraction_bps": maintenance_margin_fraction_bps,
        "max_profit_rate_bps": max_profit_rate_bps,
        "asset_class": asset_class,
        "allow_increase_position": allow_increase_position,
        "active": active,
        "max_skew_scale_usd": max_skew_scale_usd,
        "max_funding_rate": max_funding_rate
      },
      "market": {
        "long_position_size": long_position_size,
        "long_accum_se": long_accum_se,
        "long_accum_s2e": long_accum_s2e,
        "short_position_size": short_position_size,
        "short_accum_se": short_accum_se,
        "short_accum_s2e": short_accum_s2e,
        "current_funding_rate": current_funding_rate,
        "last_funding_time": last_funding_time,
        "accum_funding_long": accum_funding_long,
        "accum_funding_short": accum_funding_short,
        "funding_accrued": funding_accrued
      },
      "trading_config": {
        "funding_interval": funding_interval,
        "min_profit_duration": min_profit_duration,
        "dev_fee_rate_bps": dev_fee_rate_bps,
        "max_position": max_position
      },
      "asset_class_config": {
        "base_borrowing_rate": base_borrowing_rate
      },
      "asset_class": {
        "reserve_value_e30": reserve_value_e30,
        "sum_borrowing_rate": sum_borrowing_rate,
        "last_borrowing_time": last_borrowing_time,
        "sum_borrowing_fee_e30": sum_borrowing_fee_e30,
        "sum_settled_borrowing_fee_e30": sum_settled_borrowing_fee_e30
      }
    }

  def get_hlp_tvl(self):
    calls = [
      self.multicall_instance.create_call(
        self.vault_storage_instance, "hlpLiquidity", [collateral])
      for collateral in COLLATERALS
    ]
    collateral_usd = [
      self.oracle_middleware.get_price(
        COLLATERAL_ASSET_ID_MAP[collateral]) * 1e10
      for collateral in COLLATERALS
    ]
    results = self.multicall_instance.call(calls)

    return sum(
      [
        int(results[1][index].hex(), 16) *
          collateral_usd[index] // 10 ** TOKEN_PROFILE[collateral]["decimals"]
          for index, collateral in enumerate(COLLATERALS)
      ]
    )
