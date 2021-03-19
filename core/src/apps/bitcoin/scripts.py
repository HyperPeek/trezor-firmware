from trezor import utils, wire
from trezor.crypto import base58, cashaddr
from trezor.crypto.hashlib import sha256
from trezor.messages import InputScriptType

from apps.common import address_type
from apps.common.readers import read_bitcoin_varint
from apps.common.writers import empty_bytearray, write_bitcoin_varint

from . import common
from .multisig import (
    multisig_get_pubkey_count,
    multisig_get_pubkeys,
    multisig_pubkey_index,
)
from .readers import read_memoryview_prefixed, read_op_push
from .writers import (
    op_push_length,
    write_bytes_fixed,
    write_bytes_prefixed,
    write_bytes_unchecked,
    write_op_push,
)

if False:
    from typing import List, Optional, Sequence, Tuple, Union

    from trezor.messages.MultisigRedeemScriptType import MultisigRedeemScriptType
    from trezor.messages.TxInput import TxInput
    from trezor.messages.TxInput import EnumTypeInputScriptType

    from apps.common.coininfo import CoinInfo

    from .writers import Writer


def write_input_script_prefixed(
    w: Writer,
    script_type: EnumTypeInputScriptType,
    multisig: Optional[MultisigRedeemScriptType],
    coin: CoinInfo,
    hash_type: int,
    pubkey: bytes,
    signature: bytes,
) -> None:
    if script_type == InputScriptType.SPENDADDRESS:
        # p2pkh or p2sh
        write_input_script_p2pkh_or_p2sh_prefixed(w, pubkey, signature, hash_type)
    elif script_type == InputScriptType.SPENDP2SHWITNESS:
        # p2wpkh or p2wsh using p2sh

        if multisig is not None:
            # p2wsh in p2sh
            pubkeys = multisig_get_pubkeys(multisig)
            witness_script_h = utils.HashWriter(sha256())
            write_output_script_multisig(witness_script_h, pubkeys, multisig.m)
            script_sig = input_script_p2wsh_in_p2sh(witness_script_h.get_digest())
            write_bytes_prefixed(w, script_sig)
        else:
            # p2wpkh in p2sh
            script_sig = input_script_p2wpkh_in_p2sh(
                common.ecdsa_hash_pubkey(pubkey, coin)
            )
            write_bytes_prefixed(w, script_sig)
    elif script_type == InputScriptType.SPENDWITNESS:
        # native p2wpkh or p2wsh
        script_sig = input_script_native_p2wpkh_or_p2wsh()
        write_bytes_prefixed(w, script_sig)
    elif script_type == InputScriptType.SPENDMULTISIG:
        # p2sh multisig
        assert multisig is not None  # checked in sanitize_tx_input
        signature_index = multisig_pubkey_index(multisig, pubkey)
        write_input_script_multisig_prefixed(
            w, multisig, signature, signature_index, hash_type, coin
        )
    else:
        raise wire.ProcessError("Invalid script type")


def output_derive_script(address: str, coin: CoinInfo) -> bytes:
    if coin.bech32_prefix and address.startswith(coin.bech32_prefix):
        # p2wpkh or p2wsh
        witprog = common.decode_bech32_address(coin.bech32_prefix, address)
        return output_script_native_p2wpkh_or_p2wsh(witprog)

    if (
        not utils.BITCOIN_ONLY
        and coin.cashaddr_prefix is not None
        and address.startswith(coin.cashaddr_prefix + ":")
    ):
        prefix, addr = address.split(":")
        version, data = cashaddr.decode(prefix, addr)
        if version == cashaddr.ADDRESS_TYPE_P2KH:
            version = coin.address_type
        elif version == cashaddr.ADDRESS_TYPE_P2SH:
            version = coin.address_type_p2sh
        else:
            raise wire.DataError("Unknown cashaddr address type")
        raw_address = bytes([version]) + data
    else:
        try:
            raw_address = base58.decode_check(address, coin.b58_hash)
        except ValueError:
            raise wire.DataError("Invalid address")

    if address_type.check(coin.address_type, raw_address):
        # p2pkh
        pubkeyhash = address_type.strip(coin.address_type, raw_address)
        script = output_script_p2pkh(pubkeyhash)
        return script
    elif address_type.check(coin.address_type_p2sh, raw_address):
        # p2sh
        scripthash = address_type.strip(coin.address_type_p2sh, raw_address)
        script = output_script_p2sh(scripthash)
        return script

    raise wire.DataError("Invalid address type")


# see https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki#specification
# item 5 for details
def write_bip143_script_code_prefixed(
    w: Writer,
    txi: TxInput,
    public_keys: Sequence[Union[bytes, memoryview]],
    threshold: int,
    coin: CoinInfo,
) -> None:
    if len(public_keys) > 1:
        write_output_script_multisig_prefixed(w, public_keys, threshold)
        return

    p2pkh = (
        txi.script_type == InputScriptType.SPENDWITNESS
        or txi.script_type == InputScriptType.SPENDP2SHWITNESS
        or txi.script_type == InputScriptType.SPENDADDRESS
        or txi.script_type == InputScriptType.EXTERNAL
    )

    if p2pkh:
        # for p2wpkh in p2sh or native p2wpkh
        # the scriptCode is a classic p2pkh
        write_output_script_p2pkh_prefixed(
            w, common.ecdsa_hash_pubkey(public_keys[0], coin)
        )
    else:
        raise wire.DataError("Unknown input script type for bip143 script code")


# P2PKH, P2SH
# ===
# https://github.com/bitcoin/bips/blob/master/bip-0016.mediawiki


def write_input_script_p2pkh_or_p2sh_prefixed(
    w: Writer, pubkey: bytes, signature: bytes, hash_type: int
) -> None:
    write_bitcoin_varint(w, 1 + len(signature) + 1 + 1 + len(pubkey))
    append_signature(w, signature, hash_type)
    append_pubkey(w, pubkey)


def parse_input_script_p2pkh(script_sig: bytes) -> Tuple[memoryview, memoryview, int]:
    try:
        r = utils.BufferReader(script_sig)
        n = read_op_push(r)
        signature = r.read_memoryview(n - 1)
        hash_type = r.get()

        n = read_op_push(r)
        pubkey = r.read_memoryview()
        if len(pubkey) != n:
            raise ValueError
    except (ValueError, EOFError):
        wire.DataError("Invalid scriptSig.")

    return pubkey, signature, hash_type


def write_output_script_p2pkh(w: Writer, pubkeyhash: bytes) -> None:
    w.append(0x76)  # OP_DUP
    w.append(0xA9)  # OP_HASH160
    w.append(0x14)  # OP_DATA_20
    write_bytes_fixed(w, pubkeyhash, 20)
    w.append(0x88)  # OP_EQUALVERIFY
    w.append(0xAC)  # OP_CHECKSIG


def write_output_script_p2pkh_prefixed(w: Writer, pubkeyhash: bytes) -> None:
    utils.ensure(len(pubkeyhash) == 20)
    write_bitcoin_varint(w, 25)
    write_output_script_p2pkh(w, pubkeyhash)


def output_script_p2pkh(pubkeyhash: bytes) -> bytearray:
    utils.ensure(len(pubkeyhash) == 20)
    s = empty_bytearray(25)
    write_output_script_p2pkh(s, pubkeyhash)
    return s


def output_script_p2sh(scripthash: bytes) -> bytearray:
    # A9 14 <scripthash> 87
    utils.ensure(len(scripthash) == 20)
    s = bytearray(23)
    s[0] = 0xA9  # OP_HASH_160
    s[1] = 0x14  # pushing 20 bytes
    s[2:22] = scripthash
    s[22] = 0x87  # OP_EQUAL
    return s


# SegWit: Native P2WPKH or P2WSH
# ===
# https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki#p2wpkh
# https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki#p2wsh
#
# P2WPKH (Pay-to-Witness-Public-Key-Hash) is the segwit native P2PKH.
# Not backwards compatible.
#
# P2WSH (Pay-to-Witness-Script-Hash) is segwit native P2SH.
# Not backwards compatible.


def input_script_native_p2wpkh_or_p2wsh() -> bytearray:
    # Completely replaced by the witness and therefore empty.
    return bytearray(0)


def output_script_native_p2wpkh_or_p2wsh(witprog: bytes) -> bytearray:
    # Either:
    # 00 14 <20-byte-key-hash>
    # 00 20 <32-byte-script-hash>
    length = len(witprog)
    utils.ensure(length == 20 or length == 32)

    w = empty_bytearray(3 + length)
    w.append(0x00)  # witness version byte
    w.append(length)  # pub key hash length is 20 (P2WPKH) or 32 (P2WSH) bytes
    write_bytes_fixed(w, witprog, length)  # pub key hash
    return w


# SegWit: P2WPKH nested in P2SH
# ===
# https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki#witness-program
#
# P2WPKH is nested in P2SH to be backwards compatible.
# Uses normal P2SH output scripts.


def input_script_p2wpkh_in_p2sh(pubkeyhash: bytes) -> bytearray:
    # 16 00 14 <pubkeyhash>
    # Signature is moved to the witness.
    utils.ensure(len(pubkeyhash) == 20)

    w = empty_bytearray(3 + len(pubkeyhash))
    w.append(0x16)  # length of the data
    w.append(0x00)  # witness version byte
    w.append(0x14)  # P2WPKH witness program (pub key hash length)
    write_bytes_fixed(w, pubkeyhash, 20)  # pub key hash
    return w


# SegWit: P2WSH nested in P2SH
# ===
# https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki#p2wsh-nested-in-bip16-p2sh
#
# P2WSH is nested in P2SH to be backwards compatible.
# Uses normal P2SH output scripts.


def input_script_p2wsh_in_p2sh(script_hash: bytes) -> bytearray:
    # 22 00 20 <redeem script hash>
    # Signature is moved to the witness.

    if len(script_hash) != 32:
        raise wire.DataError("Redeem script hash should be 32 bytes long")

    w = empty_bytearray(3 + len(script_hash))
    w.append(0x22)  # length of the data
    w.append(0x00)  # witness version byte
    w.append(0x20)  # P2WSH witness program (redeem script hash length)
    write_bytes_fixed(w, script_hash, 32)
    return w


# SegWit: Witness getters
# ===


def write_witness_p2wpkh(
    w: Writer, signature: bytes, pubkey: bytes, hash_type: int
) -> None:
    write_bitcoin_varint(w, 0x02)  # num of segwit items, in P2WPKH it's always 2
    write_signature_prefixed(w, signature, hash_type)
    write_bytes_prefixed(w, pubkey)


def parse_witness_p2wpkh(witness: bytes) -> Tuple[memoryview, memoryview, int]:
    try:
        r = utils.BufferReader(witness)

        if r.get() != 2:
            # num of stack items, in P2WPKH it's always 2
            raise ValueError

        n = read_bitcoin_varint(r)
        signature = r.read_memoryview(n - 1)
        hash_type = r.get()

        pubkey = read_memoryview_prefixed(r)
        if r.remaining_count():
            raise ValueError
    except (ValueError, EOFError):
        raise wire.DataError("Invalid witness.")

    return pubkey, signature, hash_type


def write_witness_multisig(
    w: Writer,
    multisig: MultisigRedeemScriptType,
    signature: bytes,
    signature_index: int,
    hash_type: int,
) -> None:
    # get other signatures, stretch with empty bytes to the number of the pubkeys
    signatures = multisig.signatures + [b""] * (
        multisig_get_pubkey_count(multisig) - len(multisig.signatures)
    )

    # fill in our signature
    if signatures[signature_index]:
        raise wire.DataError("Invalid multisig parameters")
    signatures[signature_index] = signature

    # witness program + signatures + redeem script
    num_of_witness_items = 1 + sum(1 for s in signatures if s) + 1
    write_bitcoin_varint(w, num_of_witness_items)

    # Starts with OP_FALSE because of an old OP_CHECKMULTISIG bug, which
    # consumes one additional item on the stack:
    # https://bitcoin.org/en/developer-guide#standard-transactions
    write_bitcoin_varint(w, 0)

    for s in signatures:
        if s:
            write_signature_prefixed(w, s, hash_type)  # size of the witness included

    # redeem script
    pubkeys = multisig_get_pubkeys(multisig)
    write_output_script_multisig_prefixed(w, pubkeys, multisig.m)


def parse_witness_multisig(
    witness: bytes,
) -> Tuple[memoryview, List[Tuple[memoryview, int]]]:
    try:
        r = utils.BufferReader(witness)

        # Get number of witness stack items.
        item_count = read_bitcoin_varint(r)

        # Skip over OP_FALSE, which is due to the old OP_CHECKMULTISIG bug.
        if r.get() != 0:
            raise ValueError

        signatures = []
        for i in range(item_count - 2):
            n = read_bitcoin_varint(r)
            signature = r.read_memoryview(n - 1)
            hash_type = r.get()
            signatures.append((signature, hash_type))

        script = read_memoryview_prefixed(r)
        if r.remaining_count():
            raise ValueError
    except (ValueError, EOFError):
        raise wire.DataError("Invalid witness.")

    return script, signatures


# Multisig
# ===
#
# Used either as P2SH, P2WSH, or P2WSH nested in P2SH.


def write_input_script_multisig_prefixed(
    w: Writer,
    multisig: MultisigRedeemScriptType,
    signature: bytes,
    signature_index: int,
    hash_type: int,
    coin: CoinInfo,
) -> None:
    signatures = multisig.signatures  # other signatures
    if len(signatures[signature_index]) > 0:
        raise wire.DataError("Invalid multisig parameters")
    signatures[signature_index] = signature  # our signature

    # length of the redeem script
    pubkeys = multisig_get_pubkeys(multisig)
    redeem_script_length = output_script_multisig_length(pubkeys, multisig.m)

    # length of the result
    total_length = 1  # OP_FALSE
    for s in signatures:
        if s:
            total_length += 1 + len(s) + 1  # length, signature, hash_type
    total_length += op_push_length(redeem_script_length) + redeem_script_length
    write_bitcoin_varint(w, total_length)

    # Starts with OP_FALSE because of an old OP_CHECKMULTISIG bug, which
    # consumes one additional item on the stack:
    # https://bitcoin.org/en/developer-guide#standard-transactions
    w.append(0x00)

    for s in signatures:
        if s:
            append_signature(w, s, hash_type)

    # redeem script
    write_op_push(w, redeem_script_length)
    write_output_script_multisig(w, pubkeys, multisig.m)


def parse_input_script_multisig(
    script_sig: bytes,
) -> Tuple[memoryview, List[Tuple[memoryview, int]]]:
    try:
        r = utils.BufferReader(script_sig)

        # Skip over OP_FALSE, which is due to the old OP_CHECKMULTISIG bug.
        if r.get() != 0:
            raise ValueError

        signatures = []
        n = read_op_push(r)
        while r.remaining_count() > n:
            signature = r.read_memoryview(n - 1)
            hash_type = r.get()
            signatures.append((signature, hash_type))
            n = read_op_push(r)

        script = r.read_memoryview()
        if len(script) != n:
            raise ValueError
    except (ValueError, EOFError):
        raise wire.DataError("Invalid scriptSig.")

    return script, signatures


def output_script_multisig(pubkeys: List[bytes], m: int) -> bytearray:
    w = empty_bytearray(output_script_multisig_length(pubkeys, m))
    write_output_script_multisig(w, pubkeys, m)
    return w


def write_output_script_multisig_prefixed(
    w: Writer, pubkeys: Sequence[Union[bytes, memoryview]], m: int
) -> None:
    write_bitcoin_varint(w, output_script_multisig_length(pubkeys, m))
    write_output_script_multisig(w, pubkeys, m)


def write_output_script_multisig(
    w: Writer, pubkeys: Sequence[Union[bytes, memoryview]], m: int
) -> None:
    n = len(pubkeys)
    if n < 1 or n > 15 or m < 1 or m > 15 or m > n:
        raise wire.DataError("Invalid multisig parameters")
    for pubkey in pubkeys:
        if len(pubkey) != 33:
            raise wire.DataError("Invalid multisig parameters")

    w.append(0x50 + m)  # numbers 1 to 16 are pushed as 0x50 + value
    for p in pubkeys:
        append_pubkey(w, p)
    w.append(0x50 + n)
    w.append(0xAE)  # OP_CHECKMULTISIG


def output_script_multisig_length(
    pubkeys: Sequence[Union[bytes, memoryview]], m: int
) -> int:
    return 1 + len(pubkeys) * (1 + 33) + 1 + 1  # see output_script_multisig


def parse_output_script_multisig(script: bytes) -> Tuple[List[memoryview], int]:
    try:
        r = utils.BufferReader(script)

        threshold = r.get() - 0x50
        pubkey_count = script[-2] - 0x50

        if (
            not 1 <= threshold <= 15
            or not 1 <= pubkey_count <= 15
            or threshold > pubkey_count
        ):
            raise ValueError

        public_keys = []
        for i in range(pubkey_count):
            n = read_op_push(r)
            if n != 33:
                raise ValueError
            public_keys.append(r.read_memoryview(n))

        r.get()  # ignore pubkey_count
        if r.get() != 0xAE:  # OP_CHECKMULTISIG
            raise ValueError
        if r.remaining_count():
            raise ValueError

    except (ValueError, IndexError, EOFError):
        raise wire.DataError("Invalid multisig script")

    return public_keys, threshold


# OP_RETURN
# ===


def output_script_paytoopreturn(data: bytes) -> bytearray:
    w = empty_bytearray(1 + 5 + len(data))
    w.append(0x6A)  # OP_RETURN
    write_op_push(w, len(data))
    w.extend(data)
    return w


# BIP-322: SignatureProof container for scriptSig & witness
# ===
# https://github.com/bitcoin/bips/blob/master/bip-0322.mediawiki


def write_bip322_signature_proof(
    w: Writer,
    script_type: EnumTypeInputScriptType,
    multisig: Optional[MultisigRedeemScriptType],
    coin: CoinInfo,
    public_key: bytes,
    signature: bytes,
) -> None:
    write_input_script_prefixed(
        w, script_type, multisig, coin, common.SIGHASH_ALL, public_key, signature
    )

    if script_type in common.SEGWIT_INPUT_SCRIPT_TYPES:
        if multisig:
            # find the place of our signature based on the public key
            signature_index = multisig_pubkey_index(multisig, public_key)
            write_witness_multisig(
                w, multisig, signature, signature_index, common.SIGHASH_ALL
            )
        else:
            write_witness_p2wpkh(w, signature, public_key, common.SIGHASH_ALL)
    else:
        # Zero entries in witness stack.
        w.append(0x00)


def read_bip322_signature_proof(r: utils.BufferReader) -> Tuple[memoryview, memoryview]:
    script_sig = read_memoryview_prefixed(r)
    witness = r.read_memoryview()
    return script_sig, witness


# Helpers
# ===


def write_signature_prefixed(w: Writer, signature: bytes, hash_type: int) -> None:
    write_bitcoin_varint(w, len(signature) + 1)
    write_bytes_unchecked(w, signature)
    w.append(hash_type)


def append_signature(w: Writer, signature: bytes, hash_type: int) -> None:
    write_op_push(w, len(signature) + 1)
    write_bytes_unchecked(w, signature)
    w.append(hash_type)


def append_pubkey(w: Writer, pubkey: Union[bytes, memoryview]) -> None:
    write_op_push(w, len(pubkey))
    write_bytes_unchecked(w, pubkey)
