# -*- encoding: utf-8 -*-
'''
@author: xiaozhong
'''
import pymysql
import traceback
import sys
sys.path.append("..")
from lib.InitDB import InitMyDB
from lib.Loging import Logging

class Prepare(object):
    def __init__(self,threads,src_kwargs,des_kwargs):
        self.threads = threads
        self.db_conn_info = src_kwargs      #连接数据需要的基本信息
        self.des_conn_info = des_kwargs
        self.thread_list = []           #连接列表
        self.des_thread_list = []       #目标库链接列表
    def init_conn(self,primary_t=None):
        '''
        初始化数据库链接，所有链接添加到链接列表
        :param primary_t:
        :return:
        '''
        if primary_t:
            conn = InitMyDB(**self.db_conn_info).Init()
            if conn:
                try:
                    cur = conn.cursor()
                    state = self.__init_transaction(cur=cur,primary_t=True)
                    if state is None:
                        sys.exit()
                    self.thread_list.append({'conn': conn, 'cur': cur})
                    return conn,cur
                except pymysql.Error:
                    Logging(msg=traceback.format_exc(), level='error')
                    sys.exit()
        else:
            for i in range(self.threads-1):
                conn = InitMyDB(**self.db_conn_info).Init()
                if conn:
                    try:
                        cur = conn.cursor()
                        state = self.__init_transaction(cur=cur)
                        if state:
                            self.thread_list.append({'conn':conn,'cur':cur})
                    except:
                        Logging(msg=traceback.format_exc(),level='error')

    def init_des_conn(self,binlog=None):
        '''
        在线导出时用于初始化目标库的链接
        默认不记录binlog，需指定--binlog参数才能记录binlog
        session链接timeout时间为2147483
        :param binlog:
        :return:
        '''
        for i in range(self.threads-1):
            conn = InitMyDB(**self.des_conn_info).Init()
            if conn:
                try:
                    cur = conn.cursor()
                    if binlog is None:
                        cur.execute('set sql_log_bin=0;')
                    cur.execute('SET SESSION wait_timeout = 2147483;')
                    self.des_thread_list.append({'conn': conn, 'cur': cur})
                except:
                    Logging(msg=traceback.format_exc(), level='error')

    def master_info(self,cur):
        '''
        获取master信息
        :param cur:
        :return:
        '''
        cur.execute('SHOW MASTER STATUS')
        result = cur.fetchall()
        return result[0]['File'],result[0]['Position']

    def __init_transaction(self,cur,primary_t=None):
        '''
        初始化在线导出时源库所有链接的事务信息
        :param cur:
        :param primary_t:
        :return:
        '''
        try:
            cur.execute('SET SESSION wait_timeout = 2147483;')
            if primary_t:
                cur.execute('FLUSH TABLES WITH READ LOCK;')
            cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;')
            cur.execute('START TRANSACTION /*!40108 WITH CONSISTENT SNAPSHOT */;')
            return True
        except pymysql.Error:
            Logging(msg=traceback.format_exc(),level='error')
            return None

    def close(self,cur,conn):
        try:
            cur.close()
            conn.close()
        except:
            pass
    def get_chunks(self,cur,databases,tables):
        '''
        获取每个线程分块大小
        :param cur:
        :param databases:
        :param tables:
        :return:
        '''
        cur.execute('select count(*) as count from {}.{}'.format(databases,tables))
        result = cur.fetchall()
        total_rows = result[0]['count']
        chunk = int(total_rows / len(self.thread_list))
        return chunk


    def check_pri(self,cur,db,table):
        '''
        该函数获取查询数据时where条件的字段
        首先获取表主键，如果主键有自增字段将直接使用
        如果为组合主键且无自增字段将尝试选择单列唯一索引
        如果上述条件都不满足将不能执行在线导出
        因为在数据量很大的表上直接使用limit n,m的方式查询将非常耗时
        :param cur:
        :param db:
        :param table:
        :return:
        '''
        pri_name,pri_index_info = self.__get_pri_column_idx(cur=cur,db=db,table=table)
        if pri_name and pri_index_info:
            return pri_name,pri_index_info

        sql = 'SHOW INDEX FROM {}.{}'.format(db, table)
        cur.execute(sql)
        result = cur.fetchall()
        _tmp_key_info = {}
        if result:
            for idx in result:
                if idx['Non_unique'] == 0 and idx['Key_name'] != 'PRIMARY':
                    if idx['Key_name'] in _tmp_key_info:
                        _tmp_key_info['Key_name'] += [idx['Column_name']]
                    else:
                        _tmp_key_info['Key_name'] = [idx['Column_name']]

            uni_col_name = [_tmp_key_info[idx_name] for idx_name in _tmp_key_info if len(_tmp_key_info[idx_name]) == 1][0][0]
            uni_key_info = self.__get_col_info(cur=cur,db=db,table=table,col=uni_col_name)
            if uni_key_info:
                return uni_col_name,uni_key_info

        Logging(msg='there is no suitable index to choose from {}.{},'.format(db,table),level='error')
        sys.exit()

    def __get_pri_column_idx(self,cur,db,table):
        '''args顺序 database、tablename'''
        sql = 'select COLUMN_NAME,COLUMN_KEY,COLUMN_TYPE,EXTRA from INFORMATION_SCHEMA.COLUMNS where table_schema=%s and table_name=%s order by ORDINAL_POSITION;'
        cur.execute(sql, args=[db,table])
        result = cur.fetchall()
        pk_idex = []
        pk_name = None
        for idex, row in enumerate(result):
            '''如有自增列直接返回自增列'''
            if row['EXTRA'] == 'auto_increment':
                pk_idex.append({row['COLUMN_NAME']: idex})
                return row['COLUMN_NAME'],pk_idex
            if row['COLUMN_KEY'] == 'PRI':
                pk_idex.append({row['COLUMN_NAME']:idex})
                pk_name = row['COLUMN_NAME']
        if len(pk_idex) > 1:
            '''如果为复合主键且无自增列将返回空'''
            return None,None
        return pk_name,pk_idex

    def __get_col_info(self,cur,db,table,col):
        '''
        根据字段名获取字段所在顺序，通过该index获取对应的行值
        :param cur:
        :param db:
        :param table:
        :param col:
        :return:
        '''
        sql = 'select ORDINAL_POSITION from INFORMATION_SCHEMA.COLUMNS where table_schema=%s and table_name=%s and column_name=%s;'
        cur.execute(sql, args=[db, table, col])
        result = cur.fetchall()
        if result:
            return [{col:result[0]['ORDINAL_POSITION']}]
        else:
            return None

    def get_tables(self,cur,db):
        '''
        获取对应schema下的所有数据表名称
        :param cur:
        :param db:
        :return:
        '''
        sql = 'select table_name from information_schema.tables where table_schema = %s;'
        cur.execute(sql,db)
        result = cur.fetchall()
        _tmp = [row['table_name'] for row in result]
        return _tmp

