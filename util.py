import ujson as json
import requests
from requests_toolbelt import MultipartDecoder


def request_new_tokens(refresh_token, client_id, client_secret, write_out=None):
    """
    Contacts api.amazon.com/auth/o2/token to retrieve new access and refresh tokens

    :param refresh_token: valid refresh_token
    :param client_id: client_id
    :param client_secret: client_secret
    :param write_out: callable taking dict argument matching 'tokens.txt' schema
    :return: access_token, refresh_token
    """
    s = requests.session()
    params_dict = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret
    }
    res = s.post('https://api.amazon.com/auth/o2/token', data=params_dict,
                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    if res.status_code == 200:
        payload = json.loads(res.content.decode())
        if callable(write_out):
            write_out(payload)
        return payload.get('access_token'), payload.get('refresh_token')
    else:
        raise Exception("Failed to request new tokens: {} {}".format(res.status_code, res.content.decode()))


def is_directive(headers, data):
    """
    checks if a part (of multi-part body) looks like a directive

    directives have application/json content-type and key 'directive' in the top-level JSON payload object
    :param headers: dict of Part headers (from network, bytes key/values)
    :param data: dict part content, type depends on content-type (dict if application/json)
    :return: True if part looks like a directive, False otherwise
    """
    return b'application/json' in headers[b'Content-Type'] and 'directive' in data


def body_part_to_headers_and_data(part):
    """
    convert part (of multi-part body) to headers dict and content. de-serializes json if content-type is
    application/json.

    :param part: BodyPart decoded by MultipartDecoder
    :return: tuple pair of headers dict and content
    """
    if b'application/json' in part.headers[b'Content-Type'].lower():
        return part.headers, json.loads(part.text)
    return part.headers, part.text


def multipart_parse(data, content_type, encoding='latin1'):
    """
    parse multipart http response into headers, content tuples

    :param data: bytes http multipart response body
    :param content_type: str http response content-type
    :param encoding: str encoding to use when decoding content
    :return:
    """
    decoder = MultipartDecoder(data, content_type, encoding)
    return [body_part_to_headers_and_data(part) for part in decoder.parts]
