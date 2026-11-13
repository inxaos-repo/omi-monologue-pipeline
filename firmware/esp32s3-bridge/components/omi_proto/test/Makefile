CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O1 -g -fsanitize=address,undefined
test: test_host.c ../omi_proto.c ../include/omi_proto.h
	$(CC) $(CFLAGS) -I../include test_host.c ../omi_proto.c -o test_host
	./test_host
