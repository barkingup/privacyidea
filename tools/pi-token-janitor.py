#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# 2020-11-11 Timo Sturm <timo.sturm@netknights.it>
#            Select how to validate PSKC imports
# 2018-02-21 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#            Allow to import PSKC file
# 2017-11-21 Cornelius Kölbel <corenlius.koelbel@netknights.it>
#            export to CSV including usernames
# 2017-10-18 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#            Add token export (HOTP and TOTP) to PSKC
# 2017-05-02 Friedrich Weber <friedrich.weber@netknights.it>
#            Improve token matching
# 2017-04-25 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#
# Copyright (c) 2017, Cornelius Kölbel
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived from this
# software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import click
from click import ClickException
from dateutil import parser
from dateutil.tz import tzlocal, tzutc

from privacyidea.lib.policy import ACTION
from privacyidea.lib.utils import parse_legacy_time, to_unicode
from privacyidea.lib.importotp import export_pskc
from privacyidea.lib.token import (get_tokens, remove_token, enable_token,
                                   unassign_token, import_token,
                                   get_tokens_paginated_generator)
from privacyidea.models import Token
from privacyidea.app import create_app
import re
import sys
from yaml import safe_dump as yaml_safe_dump
from yaml import safe_load as yaml_safe_load

ALLOWED_ACTIONS = ["disable", "delete", "unassign", "mark", "export", "listuser", "tokenrealms"]

__doc__ = """
This script can be used to clean up the token database.

It can list, disable, delete or mark tokens based on

Conditions:

* last_auth
* orphaned tokens
* any tokeninfo
* unassigned token / token has no user
* tokentype

Actions:

* list
* unassign
* mark
* disable
* delete


    privacyidea-token-janitor find 
        --last_auth=10h|7d|2y
        --tokeninfo-key=<key>
        --has-not-tokeninfo-key<key>
        --has-tokeninfo-key<key>
        --tokeninfo-value=<value>
        --tokeninfo-value-greater-than=<value>
        --tokeninfo-value-less-than=<value>
        --tokeninfo-value-after=<value>
        --tokeninfo-value-before=<value>
        --assigned false
        --orphaned true
        --tokentype=<type>
        --serial=<regexp>
        --description=<regexp>

        --action={0!s}

        --set-description="new description"
        --set-tokeninfo-key=<key>
        --set-tokeninfo-value=<value>    

.. note:: If you fail to redirect the output of this command at the commandline
   to e.g. a file with a UnicodeEncodeError, you need to set the environment
   variable like this:
       export PYTHONIOENCODING=UTF-8
   Here is a good read about it:
   https://stackoverflow.com/questions/4545661/unicodedecodeerror-when-redirecting-to-file

""".format("|".join(ALLOWED_ACTIONS))

app = create_app(config_name='production', silent=True)


def _try_convert_to_integer(given_value_string):
    try:
        return int(given_value_string)
    except ValueError:
        raise ClickException('Not an integer: {}'.format(given_value_string))


def _compare_greater_than(_key, given_value_string):
    """
    :return: a function which returns True if its parameter (converted to an integer)
             is greater than *given_value_string* (converted to an integer).
    """
    given_value = _try_convert_to_integer(given_value_string)

    def comparator(value):
        try:
            return int(value) > given_value
        except ValueError:
            return False

    return comparator


def _compare_less_than(_key, given_value_string):
    """
    :return: a function which returns True if its parameter (converted to an integer)
             is less than *given_value_string* (converted to an integer).
    """
    given_value = _try_convert_to_integer(given_value_string)

    def comparator(value):
        try:
            return int(value) < given_value
        except ValueError:
            return False

    return comparator


def _parse_datetime(key, value):
    if key == ACTION.LASTAUTH:
        # Special case for last_auth: Legacy values are given in UTC time!
        last_auth = parser.parse(value)
        if not last_auth.tzinfo:
            last_auth = parser.parse(value, tzinfos=tzutc)
        return last_auth
    else:
        # Other values are given in local time
        return parser.parse(parse_legacy_time(value))


def _try_convert_to_datetime(given_value_string):
    try:
        parsed = parser.parse(given_value_string, dayfirst=False)
        if not parsed.tzinfo:
            # If not timezone is given we assume the timestamp is given in local time
            parsed = parsed.replace(tzinfo=tzlocal())
        return parsed
    except ValueError:
        raise ClickException('Not a valid datetime format: {}'.format(given_value_string))


def _compare_after(key, given_value_string):
    """
    :return: a function which returns True if its parameter (converted to a datetime) occurs after
             *given_value_string* (converted to a datetime).
    """
    given_value = _try_convert_to_datetime(given_value_string)

    def comparator(value):
        try:
            return _parse_datetime(key, value) > given_value
        except ValueError:
            return False

    return comparator


def _compare_before(key, given_value_string):
    """
    :return: a function which returns True if its parameter (converted to a datetime) occurs before
             *given_value_string* (converted to a datetime).
    """
    given_value = _try_convert_to_datetime(given_value_string)

    def comparator(value):
        try:
            return _parse_datetime(key, value) < given_value
        except ValueError:
            return False

    return comparator


def _compare_regex_or_equal(_key, given_regex):
    def comparator(value):
        if type(value) in (int, bool):
            # If the value from the database is an integer, we compare "euqals integer"
            given_value = _try_convert_to_integer(given_regex)
            return given_value == value
        else:
            # if the value from the database is a string, we compare regex
            return re.search(given_regex, value)

    return comparator


def build_tokenvalue_filter(key,
                            tokeninfo_value,
                            tokeninfo_value_greater_than,
                            tokeninfo_value_less_than,
                            tokeninfo_value_after,
                            tokeninfo_value_before):
    """
    Build and return a token value filter, which is a list of comparator functions.
    Each comparator function takes a tokeninfo value and returns True if the
    user-defined criterion matches.
    The filter matches a record if *all* comparator functions return True, i.e.
    if the conjunction of all comparators returns True.

    :param key: user-supplied --tokeninfo-key= value
    :param has_tokeninfo_key: user-supplied --has_tokeninfo_key= value
    :param has_not_tokeninfo_key: user-supplied --has_not_tokeninfo_key= value
    :param tokeninfo_value: user-supplied --tokeninfo-value= value
    :param tokeninfo_value_greater_than: user-supplied --tokeninfo-value-greater-than= value
    :param tokeninfo_value_less_than: user-supplied --tokeninfo-value-less-than= value
    :param tokeninfo_value_after: user-supplied --tokeninfo-value-after= value
    :param tokeninfo_value_before: user-supplied --tokeninfo-value-before= value
    :return: list of functions
    """
    tvfilter = []
    if tokeninfo_value:
        tvfilter.append(_compare_regex_or_equal(key, tokeninfo_value))
    if tokeninfo_value_greater_than:
        tvfilter.append(_compare_greater_than(key, tokeninfo_value_greater_than))
    if tokeninfo_value_less_than:
        tvfilter.append(_compare_less_than(key, tokeninfo_value_less_than))
    if tokeninfo_value_after:
        tvfilter.append(_compare_after(key, tokeninfo_value_after))
    if tokeninfo_value_before:
        tvfilter.append(_compare_before(key, tokeninfo_value_before))
    return tvfilter


def _get_tokenlist(last_auth, assigned, active, tokeninfo_key,
                   tokeninfo_value_filter, tokenattribute, tokenattribute_filter,
                   orphaned, tokentype, serial, description, chunksize, has_not_tokeninfo_key, has_tokeninfo_key):
    filter_active = None
    filter_assigned = None
    orphaned = orphaned or ""

    if assigned is not None:
        filter_assigned = assigned.lower() == "true"
    if active is not None:
        filter_active = active.lower() == "true"

    if chunksize is not None:
        iterable = get_tokens_paginated_generator(tokentype=tokentype,
                                                  active=filter_active,
                                                  assigned=filter_assigned,
                                                  psize=chunksize)
    else:
        iterable = [get_tokens(tokentype=tokentype,
                               active=filter_active,
                               assigned=filter_assigned)]
    for tokenobj_list in iterable:
        filtered_list = []
        sys.stderr.write("++ Creating token object list.\n")
        tok_count = 0
        tok_found = 0
        for token_obj in tokenobj_list:
            sys.stderr.write('{0} Tokens processed / {1} Tokens found\r'.format(tok_count, tok_found))
            sys.stderr.flush()
            tok_count += 1
            if last_auth and token_obj.check_last_auth_newer(last_auth):
                continue
            if serial and not re.search(serial, token_obj.token.serial):
                continue
            if description and not re.search(description,
                                             token_obj.token.description):
                continue
            if has_not_tokeninfo_key:
                if has_not_tokeninfo_key in token_obj.get_tokeninfo():
                    continue
            if has_tokeninfo_key:
                if has_tokeninfo_key not in token_obj.get_tokeninfo():
                    continue
            if tokeninfo_value_filter and tokeninfo_key:
                value = token_obj.get_tokeninfo(tokeninfo_key)
                # if the tokeninfo key is not even set, it does not match the filter
                if value is None:
                    continue
                # suppose not all comparator functions return True
                # => at least one comparator function returns False
                # => at least one user-supplied criterion does not match
                # => the token object does not match the user-supplied criteria
                if not all(comparator(value) for comparator in tokeninfo_value_filter):
                    continue
            if tokenattribute_filter and tokenattribute:
                value = token_obj.token.get(tokenattribute)
                if value is None:
                    continue
                if not all(comparator(value) for comparator in tokenattribute_filter):
                    continue
            if orphaned.upper() in ["1", "TRUE"] and not token_obj.is_orphaned():
                continue
            if orphaned.upper() in ["0", "FALSE"] and token_obj.is_orphaned():
                continue

            tok_found += 1
            # if everything matched, we append the token object
            filtered_list.append(token_obj)

        sys.stderr.write('{0} Tokens processed / {1} Tokens found\r\n'.format(tok_count, tok_found))
        sys.stderr.write("++ Token object list created.\n")
        sys.stderr.flush()
        yield filtered_list


def export_token_data(token_list, attributes=None):
    """
    Returns a list of tokens. Each token again is a simple list of data

    :param token_list:
    :param attributes: display additional user attributes
    :return:
    """
    tokens = []
    for token_obj in token_list:
        token_data = ["{0!s}".format(token_obj.token.serial),
                      "{0!s}".format(token_obj.token.tokentype)]
        try:
            user = token_obj.user
            if user:
                token_data.append("{0!s}".format(user.info.get("username", "")))
                token_data.append("{0!s}".format(user.info.get("givenname", "")))
                token_data.append("{0!s}".format(user.info.get("surname", "")))
                token_data.append("{0!s}".format(user.uid))
                token_data.append("{0!s}".format(user.resolver))
                token_data.append("{0!s}".format(user.realm))

            if attributes:
                for att in attributes.split(","):
                    token_data.append("{0!s}".format(
                        user.info.get(att, ""))
                    )
        except Exception:
            sys.stderr.write("Failed to determine user for token {0!s}.\n".format(
                token_obj.token.serial
            ))
            token_data.append("**failed to resolve user**")
        tokens.append(token_data)
    return tokens


def export_user_data(token_list, attributes=None):
    """
    Returns a list of users with the information how many tokens this user has assigned

    :param token_list:
    :param attributes: display additional user attributes
    :return:
    """
    users = {}
    for token_obj in token_list:
        try:
            user = token_obj.user
        except Exception:
            sys.stderr.write("Failed to determine user for token {0!s}.\n".format(
                token_obj.token.serial
            ))
        if user:
            uid = "'{0!s}','{1!s}','{2!s}','{3!s}','{4!s}','{5!s}'".format(
                user.info.get("username", ""),
                user.info.get("givenname", ""),
                user.info.get("surname", ""),
                user.uid,
                user.resolver,
                user.realm
            )
            if attributes:
                for att in attributes.split(","):
                    uid += ",'{0!s}'".format(
                        user.info.get(att, "")
                    )
        else:
            uid = "N/A" + ", " * 5

        if uid in users.keys():
            users[uid].append(token_obj.token.serial)
        else:
            users[uid] = [token_obj.token.serial]

    return users


@click.pass_context
@click.option('--chunksize')
def privacyidea_token_janitor(ctx, chunksize):
    """"""
    ctx.add(chunksize)


@privacyidea_token_janitor.group
@click.pass_context
@click.option('--tokeninfo', help='The tokeninfo value to match. For example: tokeninfo_key >= tokeninfo_value.'
                                  ' You can use "==", ">=" and "<="')
@click.option('--has-not-tokeninfo-key', help='filters for tokens that have not given the specified tokeninfo-key')
@click.option('--has-tokeninfo-key', help='filters for tokens that have given the specified tokeninfo-key.')
@click.option('--tokenattribute', help='Match for a certain token attribute from the database.')
@click.option('--tokentype', help='The tokentype to search.')
@click.option('--serial', help='A regular expression on the serial')
@click.option('--description', help='A regular expression on the description')
@click.option('--last_auth', help='Can be something like 10h, 7d, or 2y')
@click.option('--assigned', help='True|False|None')
@click.option('--active', help='True|False|None')
@click.option('--orphaned', help='Whether the token is an orphaned token. Set to 1')
def find(ctx, last_auth, tokeninfo, assigned,  active, orphaned, tokentype, serial, description, tokenattribute,
         has_not_tokeninfo_key, has_tokeninfo_key):
    """finds all tokens which match the conditions"""

    tokenattributes = [col.key for col in Token.__table__.columns]
    if tokenattribute and tokenattribute not in tokenattributes:
        sys.stderr.write("Unknown token attribute. Allowed attributes are {0!s}\n".format(
            ", ".join(tokenattributes)
        ))
        sys.exit(1)
    if action and action not in ALLOWED_ACTIONS:
        sys.stderr.write("Unknown action. Allowed actions are {0!s}\n".format(
            ", ".join(["'{0!s}'".format(x) for x in ALLOWED_ACTIONS])
        ))
        sys.exit(1)
    chunksize = ctx.get('chunksize')
    if chunksize is not None:
        chunksize = int(chunksize)


    # filter for tokeninfo values
    m = re.match(r"\s*[a-z][A-Z][0-9]\s*([<>=])\s*$", tokeninfo)
    tokeninfo_key = m.group(0)
    if m.group(1) == '==':
        tokeninfo_value = m.group(2)
    else:
        try:
            if m.group(1) == '>=':
                tokeninfo_value_after = m.group(2)
            elif m.group(1) == '<=':
                tokeninfo_value_before = m.group(2)
        except:
            if m.group(1) == '<=':
                tokeninfo_value_greater_than = m.group(2)
            elif m.group(1) == '>=':
                tokeninfo_value_less_than = m.group(2)
    tvfilter = build_tokenvalue_filter(tokeninfo_key,
                                       tokeninfo_value,
                                       tokeninfo_value_greater_than,
                                       tokeninfo_value_less_than,
                                       tokeninfo_value_after,
                                       tokeninfo_value_before)
    # filter for token attribute
    m = re.match(r"\s*[a-z][A-Z][0-9]\s*([<>=])\s*$", tokenattribute)
    tokenattribute_key = m.group(0)
    if m.group(1) == '==':
        tokenattribute_value = m.group(2)
    elif m.group(1) == '<=':
        tokenattribute_value_greater_than = m.group(2)
    elif m.group(1) == '>=':
        tokenattribute_value_less_than = m.group(2)
    tafilter = build_tokenvalue_filter(tokenattribute_key,
                                       tokenattribute_value,
                                       tokenattribute_value_greater_than,
                                       tokenattribute_value_less_than,
                                       None, None)
    generator = _get_tokenlist(last_auth, assigned, active,
                               tokeninfo_key, tvfilter,
                               tokenattribute, tafilter,
                               orphaned, tokentype, serial,
                               description, chunksize, has_not_tokeninfo_key,
                               has_tokeninfo_key)
    if chunksize is not None:
        sys.stderr.write("+ Reading tokens from database in chunks of {}...\n".format(chunksize))
    else:
        sys.stderr.write("+ Reading tokens from database...\n")
    ctx["generator"] = generator


@find.group
@click.pass_context
def action(ctx):
    """"""


@action.group
@click.pass_context
@click.option('--sum', help='In case of the action "listuser", this switch specifies if '
                              'the output should only contain the number of tokens owned '
                              'by the user.',
                dest='sum_tokens', action='store_true')
@click.option('--attributes', help='Extends the "listuser" function to display additional user attributes')
def listuser(ctx, sum_tokens, attributes):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        if not sum_tokens:
            tokens = export_token_data(tlist, attributes)
            for token in tokens:
                print(",".join(["'{0!s}'".format(x) for x in token]))
        else:
            users = export_user_data(tlist, attributes)
            for user, tokens in users.items():
                print("{0!s},{1!s}".format(user, len(tokens)))


@action.group
@click.pass_context
@click.option('--format', type=click.Choice(['csv', 'yaml', 'pskc']), default='pskc',
                help='In case of a simple find, the output is written as CSV instead of the '
                     'formatted output.')
@click.option('--b32', help='In case of exporting found tokens '
                            'to CSV the seed is written base32 encoded instead of hex.')
def export(ctx, export_format, b32):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        if export_format is 'csv':
            for tokenobj in tlist:
                if tokenobj.type.lower() not in ["totp", "hotp"]:
                    continue
                token_dict = tokenobj._to_dict(b32=b32)
                owner = "{!s}@{!s}".format(tokenobj.user.login, tokenobj.user.realm) if tokenobj.user else "n/a"
                if type == "totp":
                    print("{!s}, {!s}, {!s}, {!s}, {!s}, {!s}".format(owner, token_dict.get("serial"),
                                                                      token_dict.get("otpkey"),
                                                                      token_dict.get("type"),
                                                                      token_dict.get("otplen"),
                                                                      token_dict.get("timestep")))
                else:
                    print("{!s}, {!s}, {!s}, {!s}, {!s}".format(owner, token_dict.get("serial"),
                                                                token_dict.get("otpkey"),
                                                                token_dict.get("type"),
                                                                token_dict.get("otplen")))
        elif export_format is 'yaml':
            token_list = []
            for tokenobj in tlist:
                try:
                    token_dict = tokenobj._to_dict(b32=b32)
                    token_dict["owner"] = "{!s}@{!s}".format(tokenobj.user.login,
                                                             tokenobj.user.realm) if tokenobj.user else "n/a"
                    token_list.append(token_dict)
                except Exception as e:
                    sys.stderr.write("\nFailed to export token {0!s}.\n".format(token_dict.get("serial")))
            print(yaml_safe_dump(token_list))
        else:
            key, token_num, soup = export_pskc(tlist)
            sys.stderr.write("\n{0!s} tokens exported.\n".format(token_num))
            sys.stderr.write("\nThis is the AES encryption key of the token seeds.\n"
                             "You need this key to import the "
                             "tokens again:\n\n\t{0!s}\n\n".format(key))
            print("{0!s}".format(soup))


@action.group
@click.pass_context
def tokenrealms(ctx):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        for token_obj in tlist:
            try:
                trealms = [r.strip() for r in tokenrealms.split(",") if r]
                token_obj.set_realms(trealms)
                print("Setting realms of token {0!s} to {1!s}.".format(token_obj.token.serial, trealms))
            except Exception as exx:
                print("Failed to process token {0}.".format(
                    token_obj.token.serial))
                print("{0}".format(exx))


@action.group
@click.pass_context
def disable(ctx):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        for token_obj in tlist:
            try:
                enable_token(serial=token_obj.token.serial, enable=False)
                print("Disabling token {0!s}".format(token_obj.token.serial))
            except Exception as exx:
                print("Failed to process token {0}.".format(
                    token_obj.token.serial))
                print("{0}".format(exx))


@action.group
@click.pass_context
def delete(ctx):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        for token_obj in tlist:
            try:
                remove_token(serial=token_obj.token.serial)
                print("Deleting token {0!s}".format(token_obj.token.serial))
            except Exception as exx:
                print("Failed to process token {0}.".format(
                    token_obj.token.serial))
                print("{0}".format(exx))


@action.group
@click.pass_context
def unassign(ctx):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        for token_obj in tlist:
            try:
                unassign_token(serial=token_obj.token.serial)
                print("Unassigning token {0!s}".format(token_obj.token.serial))
            except Exception as exx:
                print("Failed to process token {0}.".format(
                    token_obj.token.serial))
                print("{0}".format(exx))


@action.group
@click.pass_context
@click.option('--description', help='set a new description')
def set_description(ctx, description):
    """"""
    generator = ctx.get("generator")
    for tlist in generator:
        for token_obj in tlist:
            if description:
                print("Setting description for token {0!s}: {1!s}".format(
                    token_obj.token.serial,description))
                token_obj.set_description(description)
                token_obj.save()


@action.group
@click.pass_context
@click.option('--tokeninfo', help='set a new tokeninfos')
def set_tokeninfo(ctx, tokeninfo):
    """"""
    generator = ctx.get("generator")
    tokeninfos = tokeninfo.split(",")
    for tlist in generator:
        for token in tokeninfos:
            x = token.split(":")
            tokeninfo_value = x[0]
            tokeninfo_key = x[1]
            for token_obj in tlist:
                if tokeninfo_value and tokeninfo_key:
                    print("Setting tokeninfo for token {0!s}: {1!s}={2!s}".format(
                        token_obj.token.serial, tokeninfo_key, tokeninfo_value))
                    token_obj.add_tokeninfo(tokeninfo_key, tokeninfo_value)
                    token_obj.save()


@action.group
@click.pass_context
@click.option('--tokeninfo', help='set a new tokeninfos')
def delete_tokeninfo(ctx, tokeninfo):
    """"""
    generator = ctx.get("generator")
    tokeninfos = tokeninfo.split(",")
    for tlist in generator:
        for tokeninfo in tokeninfos:
            for token_obj in tlist:
                if tokeninfo:
                    print("Deleting tokeninfo for token {0!s}: {1!s}".format(
                        token_obj.token.serial, tokeninfo))
                    token_obj.del_tokeninfo(tokeninfo)
                    token_obj.save()


@privacyidea_token_janitor.group
@click.option('--yaml', dest='yaml',
                help='Specify the YAML file with the previously exported tokens.')
def updatetokens(yaml):
    """
    This can update existing tokens in the privacyIDEA system. You can specify a yaml file with the tokendata.
    Can be used to reencrypt data, when changing the encryption key.
    """
    print("Loading YAML data. This may take a while.")
    token_list = yaml_safe_load(open(yaml, 'r').read())
    for tok in token_list:
        del(tok["owner"])
        tok_objects = get_tokens(serial=tok.get("serial"))
        if len(tok_objects) == 0:
            sys.stderr.write("\nCan not find token {0!s}. Not updating.\n".format(tok.get("serial")))
        else:
            print("Updating token {0!s}.".format(tok.get("serial")))
            try:
                tok_objects[0].update(tok)
            except Exception as e:
                sys.stderr.write("\nFailed to update token {0!s}.".format(tok.get("serial")))


@privacyidea_token_janitor.group
@click.option('--pskc', dest='pskc',
                help='Import this PSKC file.')
@click.option('--preshared_key_hex', dest='preshared_key_hex',
                help='The AES encryption key.')
@click.option('--validate_mac', dest='validate_mac', default='check_fail_hard',
                help="How the file should be validated.\n"
                     "'no_check' : Every token is parsed, ignoring HMAC\n"
                     "'check_fail_soft' : Skip tokens with invalid HMAC\n"
                     "'check_fail_hard' : Only import tokens if all HMAC are valid.")
def loadtokens(pskc, preshared_key_hex, validate_mac):
    """
    Loads token data from a PSKC file.
    """
    from privacyidea.lib.importotp import parsePSKCdata

    with open(pskc, 'r') as pskcfile:
        file_contents = pskcfile.read()

    tokens, not_parsed_tokens = parsePSKCdata(file_contents,
                                              preshared_key_hex=preshared_key_hex,
                                              validate_mac=validate_mac)
    success = 0
    failed = 0
    failed_tokens = []
    for serial in tokens:
        try:
            print("Importing token {0!s}".format(serial))
            import_token(serial, tokens[serial])
            success = success + 1
        except Exception as e:
            failed = failed + 1
            failed_tokens.append(serial)
            print("--- Failed to import token. {0!s}".format(e))

    if not_parsed_tokens:
        print("The following tokens were not read from the PSKC file"
              " because they could not be validated: {0!s}".format(not_parsed_tokens))
    print("Successfully imported {0!s} tokens.".format(success))
    print("Failed to import {0!s} tokens: {1!s}".format(failed, failed_tokens))


if __name__ == '__main__':
    privacyidea_token_janitor.run()
