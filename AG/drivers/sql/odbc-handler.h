#ifndef _ODBC_HANDLER_H_
#define _ODBC_HANDLER_H_

#include <stdlib.h>
#include <stdio.h>
#include <sql.h>
#include <sqlext.h>
#include <string.h>

#include <sstream>
#include <iostream>
#include <vector>

#include <block_index.h>

//#include "libsyndicate.h"

using namespace std;

class ODBCHandler 
{
    private:
	//ODBC components...
	SQLHENV env;
	SQLHDBC dbc;
	static ODBCHandler& odh;

	ODBCHandler();
	ODBCHandler(unsigned char* con_str);
	ODBCHandler(ODBCHandler const&);

    public:
	static  ODBCHandler&  get_handle(unsigned char* con_str);
	string  execute_query(unsigned char* sql_query, ssize_t read_size, 
				off_t byte_offset, off_t block_offset, 
				ssize_t block_size);
	string  get_tables();
	string  get_db_info();
	string  extract_error(SQLHANDLE handle, SQLSMALLINT type);
	void	encode_results(stringstream& str_stream, char* column, 
				bool row_bound);
	void    print();
};


#endif //_ODBC_HANDLER_H_
