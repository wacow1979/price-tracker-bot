# coding: utf-8
import logging
import uuid

import requests
from bs4 import BeautifulSoup

import DataSource
import PTConfig
import PTError
import Bot
from Entity import GoodInfo

good_url = PTConfig.momo_good_url()
basic_headers = {
    'User-Agent': PTConfig.USER_AGENT
}


def upsert_user(user_id, chat_id):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''INSERT INTO public."user"
    (id, chat_id)
    VALUES(%s, %s)
    ON CONFLICT(id) DO UPDATE
    SET chat_id = EXCLUDED.chat_id;
    '''
    cursor.execute(sql, (user_id, chat_id))
    conn.commit()
    pool.putconn(conn, close=True)


def add_user_good_info(user_good_info):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    cursor.execute('select * from "user" where id=%s for update;', (str(user_good_info.user_id),))
    cursor.execute('select count(1) from user_sub_good where user_id=%s', (str(user_good_info.user_id),))
    total_size = cursor.fetchone()[0]
    if total_size >= PTConfig.USER_SUB_GOOD_LIMITED:
        pool.putconn(conn, close=True)
        raise PTError.ExceedLimitedSizeError
    sql = '''INSERT INTO public.user_sub_good
    (id, user_id, good_id, price, is_notified)
    VALUES(%s, %s, %s, %s, false)
    ON CONFLICT(user_id, good_id) DO UPDATE
    SET price = EXCLUDED.price, is_notified = EXCLUDED.is_notified;
    '''
    cursor.execute(sql, (uuid.uuid4(), user_good_info.user_id, user_good_info.good_id, user_good_info.original_price))
    conn.commit()
    pool.putconn(conn, close=True)


def add_good_info(good_info):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''INSERT INTO good_info (id, name, price) VALUES(%s, %s, %s) ON CONFLICT(id) DO UPDATE
    SET name = EXCLUDED.name, price = EXCLUDED.price;
    '''
    cursor.execute(sql, (good_info.good_id, good_info.name, good_info.price))
    conn.commit()
    pool.putconn(conn, close=True)


def _get_good_info_from_momo(i_code):
    params = {'i_code': i_code}
    response = requests.request("GET", good_url, params=params, headers=basic_headers)
    return response.text


def _format_price(price):
    return int(str(price).strip().replace(',', ''))


def get_good_info(good_id):
    logging.info("good_id %s", good_id)
    response = _get_good_info_from_momo(good_id)
    soup = BeautifulSoup(response, "html.parser")
    good_name = soup.select(PTConfig.MOMO_NAME_PATH)[0].text
    logging.info("good_name %s", good_name)
    price = _get_price_by_bs4(soup)
    logging.info("price %s", price)
    return GoodInfo(good_id=good_id, name=good_name, price=price)


def _get_price_by_bs4(soup):
    try:
        return _format_price(soup.select(PTConfig.MOMO_SINGLE_PRICE_PATH)[0].text)
    except Exception as e:
        return _format_price(soup.select(PTConfig.MOMO_TWO_PRICE_PATH)[0].text)


def sync_price():
    logging.debug('price syncer started')
    for good_info in _find_all_good():
        try:
            good_id = good_info.good_id
            is_exist = _remove_redundant_good_info(good_info.good_id)
            if not is_exist:
                continue
            new_good_info = get_good_info(good_id)
            add_good_info(new_good_info)
            cheaper_records = {}
            if new_good_info.price != good_info.price:
                _reset_higher_user_sub(good_id)
                cheaper_records = _find_user_sub_goods_price_higher(new_good_info.price, good_id)
            msg = '%s\n目前價格為%s, 低於當初紀錄價格%s'
            success_notified = []
            for cheaper_record in cheaper_records:
                chat_id = cheaper_record[3]
                original_price = cheaper_record[2]
                Bot.send(msg % (new_good_info.name, new_good_info.price, original_price), chat_id)
                success_notified.append(cheaper_record[0])
        except Exception as e:
            logging.error(e)
    logging.debug('price syncer finished')


def _find_all_good():
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''select id,price,name from good_info;
        '''
    cursor.execute(sql)
    all_results = cursor.fetchall()
    pool.putconn(conn, close=True)
    goods = []
    for result in all_results:
        goods.append(GoodInfo(good_id=result[0], price=result[1], name=result[2]))
    return goods


def _remove_redundant_good_info(good_id):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''select id from user_sub_good where good_id=%s LIMIT 1;
            '''
    cursor.execute(sql, (good_id,))
    is_exist = len(cursor.fetchall()) > 0
    if is_exist:
        pool.putconn(conn, close=True)
        return True
    sql = '''DELETE FROM public.good_info
    WHERE id=%s;
    '''
    cursor.execute(sql, (good_id,))
    conn.commit()
    pool.putconn(conn, close=True)


def _find_user_sub_goods_price_higher(new_price, good_id):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''select usg.id,usg.user_id, usg.price, u.chat_id from user_sub_good usg
    join "user" u on  usg.user_id = u.id
    where usg.good_id = %s and usg.price > %s and usg.is_notified = false;
    '''
    cursor.execute(sql, (good_id, new_price))
    all_results = cursor.fetchall()
    pool.putconn(conn, close=True)
    return all_results


def _reset_higher_user_sub(good_id):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''update user_sub_good set is_notified=false where good_id=%s;
        '''
    cursor.execute(sql, (good_id,))
    conn.commit()
    pool.putconn(conn, close=True)


def _mark_is_notified_by_id(ids):
    if len(ids) < 1:
        return
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''update user_sub_good set is_notified=true where id in (%s);
        '''
    cursor.execute(sql, ids)
    conn.commit()
    pool.putconn(conn, close=True)


def find_user_sub_goods(user_id):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''select gi.name, usg.price from user_sub_good usg
        join good_info gi on gi.id = usg.good_id where usg.user_id = %s;
        '''
    cursor.execute(sql, (user_id,))
    all_results = cursor.fetchall()
    pool.putconn(conn, close=True)
    return all_results


def clear(user_id):
    pool = DataSource.get_pool()
    conn = pool.getconn()
    cursor = conn.cursor()
    sql = '''DELETE FROM public.user_sub_good
    WHERE user_id=%s;
    '''
    cursor.execute(sql, (user_id,))
    conn.commit()
    pool.putconn(conn, close=True)