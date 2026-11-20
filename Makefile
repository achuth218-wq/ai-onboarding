.PHONY: test validate build

test:
	python -m unittest discover -s tests -v

validate:
	python -m aiops validate

build:
	python -m aiops build --revision local
