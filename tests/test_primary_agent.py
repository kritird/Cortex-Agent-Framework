"""Tests for PrimaryAgent."""
import pytest
from cortex.modules.primary_agent import _parse_task_blocks, _parse_clarification


def test_parse_task_blocks():
    text = """
<task>
  <name>web_search</name>
  <instruction>Search for recent AI news</instruction>
  <depends_on></depends_on>
</task>
<task>
  <name>summarise</name>
  <instruction>Summarise the results</instruction>
  <depends_on>web_search</depends_on>
</task>
"""
    tasks = _parse_task_blocks(text)
    assert len(tasks) == 2
    assert tasks[0].task_name == "web_search"
    assert tasks[1].task_name == "summarise"
    assert "web_search" in tasks[1].depends_on


def test_parse_task_blocks_no_depends_on():
    text = "<task><name>analysis</name><instruction>Analyse data</instruction></task>"
    tasks = _parse_task_blocks(text)
    assert len(tasks) == 1
    assert tasks[0].depends_on == []


def test_parse_clarification():
    text = "I need more info. <clarification>What is the target audience?</clarification>"
    q = _parse_clarification(text)
    assert q == "What is the target audience?"


def test_parse_clarification_not_present():
    text = "No clarification needed here."
    assert _parse_clarification(text) is None
