#
# Dependencies 
#

LOG4CPLUS_DIR=$(shell pkg-config --variable=prefix log4cplus)
APR_DIR=$(shell pkg-config --variable=prefix apr-1)
# ZLOG_DIR=$(shell pkg-config --variable=prefix zlog)
CUNIT_DIR=$(shell pkg-config --variable=prefix cunit)

APPS_DIR=./examples
OUTPUT_DIR=app.rootfs
ECOA_PROJECT=marx_brothers
ECOA_PROJECT_XML_NAME=$(ECOA_PROJECT)
ECOA_PROJECT_XML_DIR=$(OUTPUT_DIR)/$(ECOA_PROJECT)
ECOA_OUTPUT_DIR=$(ECOA_PROJECT_XML_DIR)/$(shell sed -n -e 's/.*<\outputDirectory>\(.*\)<\/outputDirectory>.*/\1/p' $(APPS_DIR)/$(ECOA_PROJECT)/$(ECOA_PROJECT_XML_NAME).project.xml)
ECOA_USERID=0

all:
#
#  ECOA application
#
path_test:
	@echo "LOG4CPLUS_DIR: $(LOG4CPLUS_DIR) "
	@echo "APR_DIR: $(APR_DIR) "
# 	@echo "ZLOG_DIR: $(ZLOG_DIR) "
	@echo "CUNIT_DIR: $(CUNIT_DIR) "
	
generate_ecoa:
	@echo "Generating ECOA $(ECOA_PROJECT) app" && \
	mkdir -p $(ECOA_PROJECT_XML_DIR) && \
	cp -pr $(APPS_DIR)/$(ECOA_PROJECT)/* $(ECOA_PROJECT_XML_DIR) && \
	ecoa-ldp -v3 -k ecoa-exvt -p $(ECOA_PROJECT_XML_DIR)/$(ECOA_PROJECT_XML_NAME).project.xml -u $(ECOA_USERID)

bootstrap_ecoa:
	@echo "Bootstrapping ECOA $(ECOA_PROJECT) app"
	@cmake -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
		   -DAPR_DIR=$(APR_DIR) \
		   -DLOG4CPLUS_DIR=$(LOG4CPLUS_DIR) \
		   -DCUNIT_DIR=$(CUNIT_DIR) \
		   -B $(ECOA_OUTPUT_DIR)/build \
		   -S $(ECOA_OUTPUT_DIR) \
           -C cmake_config.cmake

all_ecoa: generate_ecoa bootstrap_ecoa
	@echo "Building ECOA $(ECOA_PROJECT) app"
	@make --no-print-directory -C $(ECOA_OUTPUT_DIR)/build all

clean_ecoa:
	@echo "Cleaning ECOA $(ECOA_PROJECT) app"
	@make --no-print-directory -C $(ECOA_OUTPUT_DIR) clean

distclean_ecoa:
	@echo "Dist cleaning ECOA $(ECOA_PROJECT) app"
	@rm -rf $(ECOA_PROJECT_XML_DIR)

run_ecoa: all_ecoa
	@echo "Running ECOA $(ECOA_PROJECT) app"
	@make --no-print-directory -C $(ECOA_OUTPUT_DIR)/build run