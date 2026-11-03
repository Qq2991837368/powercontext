/**
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "sql/ob_file_sql_rewriter.h"

#include <ctype.h>
#include <dirent.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <algorithm>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "common/object/ob_object.h"
#include "lib/allocator/ob_allocator.h"
#include "lib/oblog/ob_log_module.h"
#include "lib/string/ob_string.h"
#include "share/ob_errno.h"
#include "sql/session/ob_sql_session_info.h"

namespace oceanbase
{
namespace sql
{
namespace file_sql_detail
{

static const size_t FS_MAX_FILE_SIZE = 64L * 1024L * 1024L;
static const size_t FS_MAX_ROWS = 200000L;
static const char *FS_ENABLE_VAR_NAME = "enable_file_sql";

enum FsType
{
  FS_TYPE_UNKNOWN = 0,
  FS_TYPE_BIGINT,
  FS_TYPE_DOUBLE,
  FS_TYPE_BOOLEAN,
  FS_TYPE_VARCHAR,
  FS_TYPE_DATE,
  FS_TYPE_DATETIME
};

enum FsJsonKind
{
  FS_JSON_NULL = 0,
  FS_JSON_BOOL,
  FS_JSON_NUMBER,
  FS_JSON_STRING
};

struct FsCell
{
  bool is_null_;
  FsType type_;
  std::string text_;

  FsCell() : is_null_(true), type_(FS_TYPE_UNKNOWN), text_()
  {
  }
};

struct FsColumn
{
  std::string name_;
  FsType type_;
  bool nullable_;

  FsColumn() : name_(), type_(FS_TYPE_UNKNOWN), nullable_(false)
  {
  }
};

struct FsTable
{
  std::vector<FsColumn> columns_;
  std::vector<std::vector<FsCell> > rows_;

  FsTable() : columns_(), rows_()
  {
  }
};

struct FsJsonValue
{
  FsJsonKind kind_;
  bool bool_value_;
  std::string text_;
  FsType type_;

  FsJsonValue() : kind_(FS_JSON_NULL), bool_value_(false), text_(), type_(FS_TYPE_UNKNOWN)
  {
  }
};

struct FsCallOptions
{
  std::string path_;
  bool has_path_;
  bool header_;
  bool has_header_;
  long long sample_size_;
  bool has_sample_size_;
  std::string format_;
  bool has_format_;
  bool syntax_valid_;

  FsCallOptions()
      : path_(), has_path_(false), header_(true), has_header_(false),
        sample_size_(-1), has_sample_size_(false), format_(), has_format_(false),
        syntax_valid_(true)
  {
  }
};

static bool fs_is_space(char c)
{
  return ' ' == c || '\t' == c || '\n' == c || '\r' == c || '\f' == c || '\v' == c;
}

static bool fs_is_ident_char(char c)
{
  return isalnum(static_cast<unsigned char>(c)) || '_' == c || '$' == c;
}

static bool fs_is_ident_start(char c)
{
  return isalpha(static_cast<unsigned char>(c)) || '_' == c || '$' == c;
}

static std::string fs_trim(const std::string &s)
{
  size_t begin = 0;
  size_t end = s.size();
  while (begin < end && fs_is_space(s[begin])) {
    ++begin;
  }
  while (end > begin && fs_is_space(s[end - 1])) {
    --end;
  }
  return s.substr(begin, end - begin);
}

static std::string fs_lower(const std::string &s)
{
  std::string result = s;
  for (size_t i = 0; i < result.size(); ++i) {
    result[i] = static_cast<char>(tolower(static_cast<unsigned char>(result[i])));
  }
  return result;
}

static bool fs_ieq(const std::string &a, const char *b)
{
  size_t len = strlen(b);
  if (a.size() != len) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    if (tolower(static_cast<unsigned char>(a[i])) !=
        tolower(static_cast<unsigned char>(b[i]))) {
      return false;
    }
  }
  return true;
}

static size_t fs_skip_spaces(const std::string &s, size_t pos)
{
  while (pos < s.size() && fs_is_space(s[pos])) {
    ++pos;
  }
  return pos;
}

static bool fs_match_word(const std::string &s, size_t pos, const char *word, size_t &end)
{
  bool matched = false;
  size_t word_len = strlen(word);
  if (pos <= s.size() && pos + word_len <= s.size()) {
    matched = true;
    for (size_t i = 0; i < word_len; ++i) {
      if (tolower(static_cast<unsigned char>(s[pos + i])) !=
          tolower(static_cast<unsigned char>(word[i]))) {
        matched = false;
        break;
      }
    }
    if (matched && pos > 0 && fs_is_ident_char(s[pos - 1])) {
      matched = false;
    }
    if (matched && pos + word_len < s.size() && fs_is_ident_char(s[pos + word_len])) {
      matched = false;
    }
  }
  if (matched) {
    end = pos + word_len;
  }
  return matched;
}

static std::string fs_backquote(const std::string &name)
{
  std::string result;
  result.reserve(name.size() + 2);
  result.push_back('`');
  for (size_t i = 0; i < name.size(); ++i) {
    if ('`' == name[i]) {
      result.push_back('`');
    }
    result.push_back(name[i]);
  }
  result.push_back('`');
  return result;
}

static std::string fs_quote_string(const std::string &value)
{
  std::string result;
  result.reserve(value.size() + 2);
  result.push_back('\'');
  for (size_t i = 0; i < value.size(); ++i) {
    char c = value[i];
    switch (c) {
      case '\\': result += "\\\\"; break;
      case '\'': result += "\\'"; break;
      case '\n': result += "\\n"; break;
      case '\r': result += "\\r"; break;
      case '\t': result += "\\t"; break;
      case '\0': result += "\\0"; break;
      default: result.push_back(c); break;
    }
  }
  result.push_back('\'');
  return result;
}

static bool fs_is_digit(char c)
{
  return c >= '0' && c <= '9';
}

static bool fs_is_date_text(const std::string &s)
{
  bool result = false;
  if (s.size() == 10 && '-' == s[4] && '-' == s[7]) {
    result = true;
    for (size_t i = 0; i < s.size() && result; ++i) {
      if (4 == i || 7 == i) {
        continue;
      }
      result = fs_is_digit(s[i]);
    }
  }
  return result;
}

static bool fs_is_datetime_text(const std::string &s)
{
  bool result = false;
  if (s.size() >= 19 && '-' == s[4] && '-' == s[7] && (' ' == s[10] || 'T' == s[10] || 't' == s[10])) {
    result = true;
    for (size_t i = 0; i < 19 && result; ++i) {
      if (4 == i || 7 == i || 10 == i || 13 == i || 16 == i) {
        continue;
      }
      result = fs_is_digit(s[i]);
    }
    if (result && s[11] == s[12] && s[13] == s[14] && s[15] == s[16]) {
      result = false;
    }
    if (result && s.size() > 19) {
      size_t pos = 19;
      if ('.' == s[pos]) {
        ++pos;
        while (pos < s.size() && fs_is_digit(s[pos])) {
          ++pos;
        }
      }
      result = (pos == s.size());
    }
  }
  return result;
}

static FsType fs_infer_text_type(const std::string &raw, bool allow_bool)
{
  FsType type = FS_TYPE_VARCHAR;
  std::string s = fs_trim(raw);
  if (s.empty()) {
    return FS_TYPE_VARCHAR;
  }
  if (allow_bool) {
    std::string lower = fs_lower(s);
    if ("true" == lower || "false" == lower) {
      return FS_TYPE_BOOLEAN;
    }
  }
  size_t pos = 0;
  if (pos < s.size() && ('+' == s[pos] || '-' == s[pos])) {
    ++pos;
  }
  bool has_digit = false;
  bool has_dot = false;
  bool has_exp = false;
  while (pos < s.size() && fs_is_digit(s[pos])) {
    has_digit = true;
    ++pos;
  }
  if (pos < s.size() && '.' == s[pos]) {
    has_dot = true;
    ++pos;
    while (pos < s.size() && fs_is_digit(s[pos])) {
      has_digit = true;
      ++pos;
    }
  }
  if (has_digit && pos < s.size() && ('e' == s[pos] || 'E' == s[pos])) {
    has_exp = true;
    ++pos;
    if (pos < s.size() && ('+' == s[pos] || '-' == s[pos])) {
      ++pos;
    }
    bool exp_digit = false;
    while (pos < s.size() && fs_is_digit(s[pos])) {
      exp_digit = true;
      ++pos;
    }
    if (!exp_digit) {
      has_exp = false;
      pos = s.size() + 1;
    }
  }
  if (pos == s.size() && has_digit) {
    type = (has_dot || has_exp) ? FS_TYPE_DOUBLE : FS_TYPE_BIGINT;
  } else if (fs_is_datetime_text(s)) {
    type = FS_TYPE_DATETIME;
  } else if (fs_is_date_text(s)) {
    type = FS_TYPE_DATE;
  } else {
    type = FS_TYPE_VARCHAR;
  }
  return type;
}

static FsType fs_merge_type(FsType a, FsType b)
{
  FsType result = FS_TYPE_VARCHAR;
  if (FS_TYPE_UNKNOWN == a) {
    result = b;
  } else if (FS_TYPE_UNKNOWN == b) {
    result = a;
  } else if (a == b) {
    result = a;
  } else if ((FS_TYPE_BIGINT == a && FS_TYPE_DOUBLE == b) ||
             (FS_TYPE_DOUBLE == a && FS_TYPE_BIGINT == b)) {
    result = FS_TYPE_DOUBLE;
  } else if ((FS_TYPE_DATE == a && FS_TYPE_DATETIME == b) ||
             (FS_TYPE_DATETIME == a && FS_TYPE_DATE == b)) {
    result = FS_TYPE_DATETIME;
  } else {
    result = FS_TYPE_VARCHAR;
  }
  return result;
}

static std::string fs_norm_datetime(const std::string &raw)
{
  std::string s = fs_trim(raw);
  if (s.size() >= 11 && ('T' == s[10] || 't' == s[10])) {
    s[10] = ' ';
  }
  return s;
}

static std::string fs_cell_literal(const FsCell &cell, FsType column_type)
{
  std::string result;
  if (cell.is_null_) {
    result = "NULL";
  } else {
    std::string text = fs_trim(cell.text_);
    switch (column_type) {
      case FS_TYPE_BIGINT:
        result = text.empty() ? "0" : text;
        break;
      case FS_TYPE_DOUBLE: {
        if (text.empty()) {
          text = "0";
        }
        bool has_exp = text.find('e') != std::string::npos || text.find('E') != std::string::npos;
        if (!has_exp) {
          if ('.' == text[0]) {
            text = "0" + text;
          } else if (text.size() >= 2 && ('+' == text[0] || '-' == text[0]) && '.' == text[1]) {
            text = text.substr(0, 1) + "0" + text.substr(1);
          }
          if ('.' == text[text.size() - 1]) {
            text += "0";
          }
          text += "e0";
        }
        result = text;
        break;
      }
      case FS_TYPE_BOOLEAN: {
        std::string lower = fs_lower(text);
        result = ("true" == lower || "1" == lower) ? "1" : "0";
        break;
      }
      case FS_TYPE_DATE:
        result = fs_quote_string(text);
        break;
      case FS_TYPE_DATETIME:
        result = fs_quote_string(fs_norm_datetime(text));
        break;
      case FS_TYPE_VARCHAR:
      default:
        result = fs_quote_string(cell.text_);
        break;
    }
  }
  return result;
}

static const char *fs_type_name(FsType type)
{
  const char *name = "VARCHAR";
  switch (type) {
    case FS_TYPE_BIGINT: name = "BIGINT"; break;
    case FS_TYPE_DOUBLE: name = "DOUBLE"; break;
    case FS_TYPE_BOOLEAN: name = "BOOLEAN"; break;
    case FS_TYPE_DATE: name = "DATE"; break;
    case FS_TYPE_DATETIME: name = "DATETIME"; break;
    case FS_TYPE_VARCHAR:
    default: name = "VARCHAR"; break;
  }
  return name;
}

static int fs_check_path(const std::string &path)
{
  int ret = OB_SUCCESS;
  if (path.empty() || path.size() > 4096 || std::string::npos != path.find('\0')) {
    ret = OB_INVALID_ARGUMENT;
  } else if (std::string::npos != path.find("..")) {
    ret = OB_INVALID_ARGUMENT;
  }
  return ret;
}

static int fs_read_file(const std::string &path, std::string &data)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(fs_check_path(path))) {
  } else {
    FILE *fp = fopen(path.c_str(), "rb");
    if (NULL == fp) {
      ret = OB_FILE_NOT_EXIST;
    } else {
      char buffer[8192];
      size_t read_size = 0;
      while (OB_SUCC(ret) && (read_size = fread(buffer, 1, sizeof(buffer), fp)) > 0) {
        if (data.size() + read_size > FS_MAX_FILE_SIZE) {
          ret = OB_SIZE_OVERFLOW;
        } else {
          data.append(buffer, read_size);
        }
      }
      if (OB_SUCC(ret) && 0 != ferror(fp)) {
        ret = OB_IO_ERROR;
      }
      fclose(fp);
    }
  }
  return ret;
}

static bool fs_parse_sql_string(const std::string &sql, size_t &pos, std::string &value)
{
  bool result = false;
  value.clear();
  if (pos < sql.size() && '\'' == sql[pos]) {
    ++pos;
    while (pos < sql.size()) {
      char c = sql[pos];
      if ('\\' == c && pos + 1 < sql.size()) {
        char next = sql[pos + 1];
        switch (next) {
          case 'n': value.push_back('\n'); break;
          case 'r': value.push_back('\r'); break;
          case 't': value.push_back('\t'); break;
          case '0': value.push_back('\0'); break;
          case 'b': value.push_back('\b'); break;
          default: value.push_back(next); break;
        }
        pos += 2;
      } else if ('\'' == c) {
        if (pos + 1 < sql.size() && '\'' == sql[pos + 1]) {
          value.push_back('\'');
          pos += 2;
        } else {
          ++pos;
          result = true;
          break;
        }
      } else {
        value.push_back(c);
        ++pos;
      }
    }
  }
  return result;
}

static bool fs_find_matching_paren(const std::string &sql, size_t open_pos, size_t &close_pos)
{
  bool result = false;
  int depth = 0;
  size_t pos = open_pos;
  while (pos < sql.size()) {
    char c = sql[pos];
    if ('\'' == c || '"' == c || '`' == c) {
      char quote = c;
      ++pos;
      while (pos < sql.size()) {
        char q = sql[pos];
        if ('\\' == q && '`' != quote && pos + 1 < sql.size()) {
          pos += 2;
        } else if (q == quote) {
          if (pos + 1 < sql.size() && sql[pos + 1] == quote) {
            pos += 2;
          } else {
            ++pos;
            break;
          }
        } else {
          ++pos;
        }
      }
      continue;
    } else if ('-' == c && pos + 1 < sql.size() && '-' == sql[pos + 1]) {
      pos += 2;
      while (pos < sql.size() && '\n' != sql[pos]) {
        ++pos;
      }
      continue;
    } else if ('/' == c && pos + 1 < sql.size() && '*' == sql[pos + 1]) {
      pos += 2;
      while (pos + 1 < sql.size() && !('/' == sql[pos] && '*' == sql[pos + 1])) {
        ++pos;
      }
      pos = (pos + 1 < sql.size()) ? pos + 2 : sql.size();
      continue;
    } else if ('(' == c) {
      ++depth;
    } else if (')' == c) {
      --depth;
      if (0 == depth) {
        close_pos = pos;
        result = true;
        break;
      }
    }
    ++pos;
  }
  return result;
}

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------
static void fs_parse_csv_records(const std::string &data,
                                 std::vector<std::vector<FsCell> > &records)
{
  std::vector<FsCell> record;
  std::string field;
  bool in_quotes = false;
  bool field_quoted = false;
  size_t pos = 0;
  while (pos < data.size()) {
    char c = data[pos];
    if (in_quotes) {
      if ('"' == c) {
        if (pos + 1 < data.size() && '"' == data[pos + 1]) {
          field.push_back('"');
          pos += 2;
        } else {
          in_quotes = false;
          ++pos;
        }
      } else {
        field.push_back(c);
        ++pos;
      }
    } else if ('"' == c && field.empty()) {
      in_quotes = true;
      field_quoted = true;
      ++pos;
    } else if (',' == c) {
      FsCell cell;
      cell.is_null_ = !field_quoted && field.empty();
      cell.text_ = field;
      record.push_back(cell);
      field.clear();
      field_quoted = false;
      ++pos;
    } else if ('\n' == c) {
      FsCell cell;
      cell.is_null_ = !field_quoted && field.empty();
      cell.text_ = field;
      record.push_back(cell);
      records.push_back(record);
      record.clear();
      field.clear();
      field_quoted = false;
      ++pos;
    } else if ('\r' == c) {
      if (pos + 1 < data.size() && '\n' == data[pos + 1]) {
        ++pos;
      } else {
        FsCell cell;
        cell.is_null_ = !field_quoted && field.empty();
        cell.text_ = field;
        record.push_back(cell);
        records.push_back(record);
        record.clear();
        field.clear();
        field_quoted = false;
        ++pos;
      }
    } else {
      field.push_back(c);
      ++pos;
    }
  }
  if (!field.empty() || field_quoted || !record.empty()) {
    FsCell cell;
    cell.is_null_ = !field_quoted && field.empty();
    cell.text_ = field;
    record.push_back(cell);
    records.push_back(record);
  }
}

static int fs_build_csv_table(const std::string &path, bool header, FsTable &table)
{
  int ret = OB_SUCCESS;
  std::string data;
  if (OB_FAIL(fs_read_file(path, data))) {
  } else {
    std::vector<std::vector<FsCell> > records;
    fs_parse_csv_records(data, records);
    size_t width = 0;
    for (size_t i = 0; i < records.size(); ++i) {
      width = std::max(width, records[i].size());
    }
    size_t data_begin = 0;
    if (header && !records.empty()) {
      data_begin = 1;
    }
    std::map<std::string, int> name_count;
    for (size_t i = 0; i < width; ++i) {
      std::string base;
      if (header && i < records[0].size() && !records[0][i].is_null_) {
        base = records[0][i].text_;
      }
      if (base.empty()) {
        base = "column" + std::to_string(i + 1);
      }
      int count = name_count[base];
      name_count[base] = count + 1;
      FsColumn column;
      if (0 == count) {
        column.name_ = base;
      } else {
        column.name_ = base + "__" + std::to_string(count + 1);
      }
      column.type_ = FS_TYPE_UNKNOWN;
      column.nullable_ = false;
      table.columns_.push_back(column);
    }
    for (size_t r = data_begin; r < records.size() && OB_SUCC(ret); ++r) {
      if (table.rows_.size() >= FS_MAX_ROWS) {
        ret = OB_SIZE_OVERFLOW;
        break;
      }
      std::vector<FsCell> row;
      row.resize(width);
      for (size_t c = 0; c < width; ++c) {
        if (c < records[r].size()) {
          row[c] = records[r][c];
        }
        if (row[c].is_null_) {
          table.columns_[c].nullable_ = true;
        } else {
          FsType value_type = fs_infer_text_type(row[c].text_, true);
          table.columns_[c].type_ =
              fs_merge_type(table.columns_[c].type_, value_type);
        }
      }
      table.rows_.push_back(row);
    }
    for (size_t c = 0; c < table.columns_.size() && OB_SUCC(ret); ++c) {
      if (FS_TYPE_UNKNOWN == table.columns_[c].type_) {
        table.columns_[c].type_ = FS_TYPE_VARCHAR;
      }
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// JSON (newline delimited)
// ---------------------------------------------------------------------------
static void fs_json_skip_ws(const std::string &s, size_t &pos)
{
  while (pos < s.size() && fs_is_space(s[pos])) {
    ++pos;
  }
}

static bool fs_json_parse_string(const std::string &s, size_t &pos, std::string &value)
{
  bool result = false;
  value.clear();
  if (pos < s.size() && '"' == s[pos]) {
    ++pos;
    result = true;
    while (pos < s.size()) {
      char c = s[pos];
      if ('\\' == c && pos + 1 < s.size()) {
        char next = s[pos + 1];
        switch (next) {
          case '"': value.push_back('"'); break;
          case '\\': value.push_back('\\'); break;
          case '/': value.push_back('/'); break;
          case 'b': value.push_back('\b'); break;
          case 'f': value.push_back('\f'); break;
          case 'n': value.push_back('\n'); break;
          case 'r': value.push_back('\r'); break;
          case 't': value.push_back('\t'); break;
          case 'u': {
            if (pos + 5 < s.size()) {
              int code = 0;
              bool ok = true;
              for (int k = 0; k < 4 && ok; ++k) {
                char h = s[pos + 2 + k];
                code <<= 4;
                if (h >= '0' && h <= '9') {
                  code += h - '0';
                } else if (h >= 'a' && h <= 'f') {
                  code += h - 'a' + 10;
                } else if (h >= 'A' && h <= 'F') {
                  code += h - 'A' + 10;
                } else {
                  ok = false;
                }
              }
              if (ok) {
                if (code < 0x80) {
                  value.push_back(static_cast<char>(code));
                } else if (code < 0x800) {
                  value.push_back(static_cast<char>(0xC0 | (code >> 6)));
                  value.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                } else {
                  value.push_back(static_cast<char>(0xE0 | (code >> 12)));
                  value.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                  value.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                }
                pos += 4;
              }
            }
            break;
          }
          default: value.push_back(next); break;
        }
        pos += 2;
      } else if ('"' == c) {
        ++pos;
        break;
      } else {
        value.push_back(c);
        ++pos;
      }
    }
  }
  return result;
}

static bool fs_json_parse_value(const std::string &s, size_t &pos, FsJsonValue &value);

static bool fs_json_skip_value(const std::string &s, size_t &pos)
{
  bool result = false;
  fs_json_skip_ws(s, pos);
  if (pos >= s.size()) {
  } else if ('"' == s[pos]) {
    std::string dummy;
    result = fs_json_parse_string(s, pos, dummy);
  } else if ('{' == s[pos]) {
    ++pos;
    fs_json_skip_ws(s, pos);
    result = true;
    while (pos < s.size() && '}' != s[pos] && result) {
      std::string key;
      result = fs_json_parse_string(s, pos, key);
      if (result) {
        fs_json_skip_ws(s, pos);
        if (pos < s.size() && ':' == s[pos]) {
          ++pos;
          result = fs_json_skip_value(s, pos);
          fs_json_skip_ws(s, pos);
          if (result && pos < s.size() && ',' == s[pos]) {
            ++pos;
            fs_json_skip_ws(s, pos);
          }
        } else {
          result = false;
        }
      }
    }
    if (result && pos < s.size() && '}' == s[pos]) {
      ++pos;
    } else {
      result = false;
    }
  } else if ('[' == s[pos]) {
    ++pos;
    fs_json_skip_ws(s, pos);
    result = true;
    while (pos < s.size() && ']' != s[pos] && result) {
      result = fs_json_skip_value(s, pos);
      fs_json_skip_ws(s, pos);
      if (result && pos < s.size() && ',' == s[pos]) {
        ++pos;
        fs_json_skip_ws(s, pos);
      }
    }
    if (result && pos < s.size() && ']' == s[pos]) {
      ++pos;
    } else {
      result = false;
    }
  } else {
    while (pos < s.size() && (fs_is_ident_char(s[pos]) || '.' == s[pos] ||
                              '+' == s[pos] || '-' == s[pos])) {
      ++pos;
    }
    result = true;
  }
  return result;
}

static bool fs_json_parse_value(const std::string &s, size_t &pos, FsJsonValue &value)
{
  bool result = false;
  fs_json_skip_ws(s, pos);
  value = FsJsonValue();
  if (pos >= s.size()) {
  } else if ('"' == s[pos]) {
    value.kind_ = FS_JSON_STRING;
    result = fs_json_parse_string(s, pos, value.text_);
    if (result) {
      value.type_ = fs_infer_text_type(value.text_, false);
    }
  } else if ('{' == s[pos] || '[' == s[pos]) {
    value.kind_ = FS_JSON_NULL;
    result = fs_json_skip_value(s, pos);
  } else {
    size_t begin = pos;
    while (pos < s.size() && (fs_is_ident_char(s[pos]) || '.' == s[pos] ||
                              '+' == s[pos] || '-' == s[pos])) {
      ++pos;
    }
    std::string token = s.substr(begin, pos - begin);
    if ("true" == token || "false" == token) {
      value.kind_ = FS_JSON_BOOL;
      value.bool_value_ = ("true" == token);
      value.type_ = FS_TYPE_BOOLEAN;
      result = true;
    } else if ("null" == token) {
      value.kind_ = FS_JSON_NULL;
      result = true;
    } else if (!token.empty()) {
      FsType type = fs_infer_text_type(token, false);
      if (FS_TYPE_BIGINT != type && FS_TYPE_DOUBLE != type) {
        result = false;
      } else {
        value.kind_ = FS_JSON_NUMBER;
        value.text_ = token;
        value.type_ = type;
        result = true;
      }
    }
  }
  return result;
}

static bool fs_json_parse_object(const std::string &line,
                                 std::vector<std::pair<std::string, FsJsonValue> > &fields)
{
  bool result = false;
  fields.clear();
  size_t pos = 0;
  fs_json_skip_ws(line, pos);
  if (pos < line.size() && '{' == line[pos]) {
    ++pos;
    fs_json_skip_ws(line, pos);
    result = true;
    bool first = true;
    while (pos < line.size() && '}' != line[pos] && result) {
      if (!first) {
        if (',' == line[pos]) {
          ++pos;
          fs_json_skip_ws(line, pos);
        } else {
          result = false;
          break;
        }
      }
      first = false;
      std::string key;
      result = fs_json_parse_string(line, pos, key);
      if (result) {
        fs_json_skip_ws(line, pos);
        if (pos < line.size() && ':' == line[pos]) {
          ++pos;
          FsJsonValue value;
          result = fs_json_parse_value(line, pos, value);
          if (result) {
            fields.push_back(std::make_pair(key, value));
            fs_json_skip_ws(line, pos);
          }
        } else {
          result = false;
        }
      }
    }
  }
  return result;
}

static int fs_build_json_table(const std::string &path, FsTable &table)
{
  int ret = OB_SUCCESS;
  std::string data;
  if (OB_FAIL(fs_read_file(path, data))) {
  } else {
    std::vector<std::vector<std::pair<std::string, FsJsonValue> > > objects;
    size_t pos = 0;
    while (pos <= data.size() && OB_SUCC(ret)) {
      size_t line_end = data.find('\n', pos);
      if (std::string::npos == line_end) {
        line_end = data.size();
      }
      std::string line = data.substr(pos, line_end - pos);
      line = fs_trim(line);
      pos = line_end + 1;
      if (line.empty()) {
        continue;
      }
      if (objects.size() >= FS_MAX_ROWS) {
        ret = OB_SIZE_OVERFLOW;
        break;
      }
      std::vector<std::pair<std::string, FsJsonValue> > fields;
      if (!fs_json_parse_object(line, fields)) {
        ret = OB_INVALID_ARGUMENT;
        break;
      }
      objects.push_back(fields);
    }
    std::vector<std::string> keys;
    std::set<std::string> key_set;
    for (size_t i = 0; i < objects.size(); ++i) {
      for (size_t j = 0; j < objects[i].size(); ++j) {
        const std::string &key = objects[i][j].first;
        if (key_set.find(key) == key_set.end()) {
          key_set.insert(key);
          keys.push_back(key);
        }
      }
    }
    for (size_t k = 0; k < keys.size() && OB_SUCC(ret); ++k) {
      FsColumn column;
      column.name_ = keys[k];
      column.type_ = FS_TYPE_UNKNOWN;
      column.nullable_ = false;
      table.columns_.push_back(column);
    }
    for (size_t i = 0; i < objects.size() && OB_SUCC(ret); ++i) {
      std::vector<FsCell> row;
      row.resize(keys.size());
      for (size_t k = 0; k < keys.size(); ++k) {
        for (size_t j = 0; j < objects[i].size(); ++j) {
          if (objects[i][j].first == keys[k]) {
            FsJsonValue &value = objects[i][j].second;
            if (FS_JSON_NULL == value.kind_) {
              row[k].is_null_ = true;
            } else {
              row[k].is_null_ = false;
              row[k].text_ = (FS_JSON_BOOL == value.kind_)
                                 ? (value.bool_value_ ? "true" : "false")
                                 : value.text_;
              row[k].type_ = value.type_;
            }
            break;
          }
        }
        if (row[k].is_null_) {
          table.columns_[k].nullable_ = true;
        } else {
          table.columns_[k].type_ =
              fs_merge_type(table.columns_[k].type_, row[k].type_);
        }
      }
      table.rows_.push_back(row);
    }
    for (size_t k = 0; k < table.columns_.size() && OB_SUCC(ret); ++k) {
      if (FS_TYPE_UNKNOWN == table.columns_[k].type_) {
        table.columns_[k].type_ = FS_TYPE_VARCHAR;
      }
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// GLOB
// ---------------------------------------------------------------------------
static bool fs_match_mask(const std::string &mask, size_t mpos, const std::string &name, size_t npos)
{
  bool result = false;
  if (mpos == mask.size()) {
    result = (npos == name.size());
  } else if ('*' == mask[mpos]) {
    for (size_t i = npos; i <= name.size() && !result; ++i) {
      result = fs_match_mask(mask, mpos + 1, name, i);
    }
  } else if ('?' == mask[mpos]) {
    result = (npos < name.size()) && fs_match_mask(mask, mpos + 1, name, npos + 1);
  } else {
    result = (npos < name.size()) && (mask[mpos] == name[npos]) &&
             fs_match_mask(mask, mpos + 1, name, npos + 1);
  }
  return result;
}

// ---------------------------------------------------------------------------
// Minimal parquet reader (PLAIN encoding, uncompressed or snappy pages)
// ---------------------------------------------------------------------------
static uint32_t fs_le32(const unsigned char *p)
{
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

static int fs_snappy_decompress(const unsigned char *src, size_t src_size,
                                std::string &out)
{
  int ret = OB_SUCCESS;
  size_t pos = 0;
  while (pos < src_size && OB_SUCC(ret)) {
    unsigned char tag = src[pos++];
    int type = tag & 3;
    if (0 == type) {
      size_t len = (tag >> 2) + 1;
      if ((tag >> 2) >= 60) {
        int extra = (tag >> 2) - 59;
        size_t value = 0;
        for (int i = 0; i < extra; ++i) {
          if (pos >= src_size) {
            ret = OB_INVALID_DATA;
            break;
          }
          value |= static_cast<size_t>(src[pos++]) << (8 * i);
        }
        len = value + 1;
      }
      if (OB_SUCC(ret) && pos + len > src_size) {
        ret = OB_INVALID_DATA;
      }
      for (size_t i = 0; i < len && OB_SUCC(ret); ++i) {
        out.push_back(static_cast<char>(src[pos++]));
      }
    } else {
      size_t len = 0;
      size_t offset = 0;
      if (1 == type) {
        len = 4 + ((tag >> 2) & 7);
        if (pos >= src_size) {
          ret = OB_INVALID_DATA;
        } else {
          offset = (static_cast<size_t>(tag >> 5) << 8) + src[pos++];
        }
      } else if (2 == type) {
        len = 1 + (tag >> 2);
        if (pos + 2 > src_size) {
          ret = OB_INVALID_DATA;
        } else {
          offset = src[pos] | (static_cast<size_t>(src[pos + 1]) << 8);
          pos += 2;
        }
      } else {
        len = 1 + (tag >> 2);
        if (pos + 4 > src_size) {
          ret = OB_INVALID_DATA;
        } else {
          offset = fs_le32(src + pos);
          pos += 4;
        }
      }
      if (OB_SUCC(ret) && (0 == offset || offset > out.size())) {
        ret = OB_INVALID_DATA;
      }
      for (size_t i = 0; i < len && OB_SUCC(ret); ++i) {
        out.push_back(out[out.size() - offset]);
      }
    }
  }
  return ret;
}

class FsCompactReader
{
public:
  FsCompactReader(const unsigned char *data, size_t size)
      : data_(data), pos_(0), size_(size)
  {
  }

  size_t offset() const { return pos_; }
  bool eof() const { return pos_ >= size_; }

  int readByte(unsigned char &value)
  {
    int ret = OB_SUCCESS;
    if (pos_ >= size_) {
      ret = OB_INVALID_DATA;
    } else {
      value = data_[pos_++];
    }
    return ret;
  }

  int readVarint(uint64_t &value)
  {
    int ret = OB_SUCCESS;
    value = 0;
    int shift = 0;
    bool finished = false;
    while (OB_SUCC(ret) && !finished && shift < 64) {
      unsigned char byte = 0;
      if (OB_FAIL(readByte(byte))) {
      } else {
        value |= static_cast<uint64_t>(byte & 0x7f) << shift;
        shift += 7;
        finished = (0 == (byte & 0x80));
      }
    }
    if (OB_SUCC(ret) && !finished) {
      ret = OB_INVALID_DATA;
    }
    return ret;
  }

  int readZigzag(int64_t &value)
  {
    uint64_t raw = 0;
    int ret = readVarint(raw);
    if (OB_SUCC(ret)) {
      value = static_cast<int64_t>(raw >> 1) ^ -static_cast<int64_t>(raw & 1);
    }
    return ret;
  }

  int readI32(int32_t &value)
  {
    int64_t raw = 0;
    int ret = readZigzag(raw);
    if (OB_SUCC(ret)) {
      value = static_cast<int32_t>(raw);
    }
    return ret;
  }

  int readI64(int64_t &value)
  {
    return readZigzag(value);
  }

  int readBinary(std::string &value)
  {
    int ret = OB_SUCCESS;
    uint64_t len = 0;
    value.clear();
    if (OB_FAIL(readVarint(len))) {
    } else if (len > size_ - pos_) {
      ret = OB_INVALID_DATA;
    } else {
      value.assign(reinterpret_cast<const char *>(data_ + pos_), static_cast<size_t>(len));
      pos_ += static_cast<size_t>(len);
    }
    return ret;
  }

  int readFieldHeader(unsigned char &type, int16_t &field_id, int16_t &last_id)
  {
    int ret = OB_SUCCESS;
    unsigned char header = 0;
    if (OB_FAIL(readByte(header))) {
    } else if (0 == header) {
      type = 0;
    } else {
      type = header & 0x0f;
      int16_t delta = static_cast<int16_t>(header >> 4);
      if (0 == delta) {
        int64_t id = 0;
        if (OB_FAIL(readZigzag(id))) {
        } else {
          field_id = static_cast<int16_t>(id);
        }
      } else {
        field_id = static_cast<int16_t>(last_id + delta);
      }
      if (OB_SUCC(ret)) {
        last_id = field_id;
      }
    }
    return ret;
  }

  int readListHeader(unsigned char &elem_type, uint32_t &count)
  {
    int ret = OB_SUCCESS;
    unsigned char header = 0;
    if (OB_FAIL(readByte(header))) {
    } else {
      count = header >> 4;
      elem_type = header & 0x0f;
      if (15 == count) {
        uint64_t size = 0;
        if (OB_FAIL(readVarint(size))) {
        } else {
          count = static_cast<uint32_t>(size);
        }
      }
    }
    return ret;
  }

  int skip(unsigned char type, int depth)
  {
    int ret = OB_SUCCESS;
    if (depth > 30) {
      ret = OB_INVALID_DATA;
    } else if (1 == type || 2 == type) {
      // bool value is encoded in the field header
    } else if (3 == type) {
      unsigned char byte = 0;
      ret = readByte(byte);
    } else if (4 == type || 5 == type || 6 == type) {
      int64_t value = 0;
      ret = readZigzag(value);
    } else if (7 == type) {
      if (pos_ + 8 > size_) {
        ret = OB_INVALID_DATA;
      } else {
        pos_ += 8;
      }
    } else if (8 == type) {
      std::string value;
      ret = readBinary(value);
    } else if (9 == type || 10 == type) {
      unsigned char elem_type = 0;
      uint32_t count = 0;
      if (OB_FAIL(readListHeader(elem_type, count))) {
      } else {
        for (uint32_t i = 0; i < count && OB_SUCC(ret); ++i) {
          ret = skip(elem_type, depth + 1);
        }
      }
    } else if (11 == type) {
      uint64_t count = 0;
      if (OB_FAIL(readVarint(count))) {
      } else if (count > 0) {
        unsigned char header = 0;
        if (OB_FAIL(readByte(header))) {
        } else {
          unsigned char key_type = header >> 4;
          unsigned char value_type = header & 0x0f;
          for (uint64_t i = 0; i < count && OB_SUCC(ret); ++i) {
            ret = skip(key_type, depth + 1);
            if (OB_SUCC(ret)) {
              ret = skip(value_type, depth + 1);
            }
          }
        }
      }
    } else if (12 == type) {
      int16_t last_id = 0;
      unsigned char field_type = 0;
      int16_t field_id = 0;
      bool finished = false;
      while (OB_SUCC(ret) && !finished) {
        if (OB_FAIL(readFieldHeader(field_type, field_id, last_id))) {
        } else if (0 == field_type) {
          finished = true;
        } else {
          ret = skip(field_type, depth + 1);
        }
      }
    } else {
      ret = OB_INVALID_DATA;
    }
    return ret;
  }

private:
  const unsigned char *data_;
  size_t pos_;
  size_t size_;
};

struct FsPqSchemaElement
{
  std::string name_;
  int32_t type_;
  int32_t repetition_;
  int32_t num_children_;
  int32_t converted_type_;

  FsPqSchemaElement()
      : name_(), type_(-1), repetition_(-1), num_children_(0), converted_type_(-1)
  {
  }
};

struct FsPqColumnMeta
{
  int32_t type_;
  int32_t codec_;
  int64_t num_values_;
  int64_t data_page_offset_;
  int64_t dictionary_page_offset_;
  int64_t total_compressed_size_;
  std::vector<std::string> path_;

  FsPqColumnMeta()
      : type_(-1), codec_(0), num_values_(0), data_page_offset_(-1),
        dictionary_page_offset_(-1), total_compressed_size_(0), path_()
  {
  }
};

struct FsPqRowGroup
{
  int64_t num_rows_;
  std::vector<FsPqColumnMeta> columns_;

  FsPqRowGroup() : num_rows_(0), columns_()
  {
  }
};

struct FsPqFileMeta
{
  int64_t num_rows_;
  std::vector<FsPqSchemaElement> schema_;
  std::vector<FsPqRowGroup> row_groups_;

  FsPqFileMeta() : num_rows_(0), schema_(), row_groups_()
  {
  }
};

static int fs_pq_parse_schema_element(FsCompactReader &reader, FsPqSchemaElement &element)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      ret = reader.readI32(element.type_);
    } else if (2 == field_id && 5 == field_type) {
      int32_t type_length = 0;
      ret = reader.readI32(type_length);
    } else if (3 == field_id && 5 == field_type) {
      ret = reader.readI32(element.repetition_);
    } else if (4 == field_id && 8 == field_type) {
      std::string name;
      if (OB_FAIL(reader.readBinary(name))) {
      } else {
        element.name_ = name;
      }
    } else if (5 == field_id && 5 == field_type) {
      ret = reader.readI32(element.num_children_);
    } else if (6 == field_id && 5 == field_type) {
      ret = reader.readI32(element.converted_type_);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_column_meta(FsCompactReader &reader, FsPqColumnMeta &meta)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      ret = reader.readI32(meta.type_);
    } else if (3 == field_id && 9 == field_type) {
      unsigned char elem_type = 0;
      uint32_t count = 0;
      if (OB_FAIL(reader.readListHeader(elem_type, count))) {
      } else if (count > 0 && 8 != elem_type) {
        ret = OB_INVALID_DATA;
      } else {
        for (uint32_t i = 0; i < count && OB_SUCC(ret); ++i) {
          std::string name;
          if (OB_FAIL(reader.readBinary(name))) {
          } else {
            meta.path_.push_back(name);
          }
        }
      }
    } else if (4 == field_id && 5 == field_type) {
      ret = reader.readI32(meta.codec_);
    } else if (5 == field_id && 6 == field_type) {
      ret = reader.readI64(meta.num_values_);
    } else if (6 == field_id && 6 == field_type) {
      int64_t total_uncompressed_size = 0;
      ret = reader.readI64(total_uncompressed_size);
    } else if (7 == field_id && 6 == field_type) {
      ret = reader.readI64(meta.total_compressed_size_);
    } else if (9 == field_id && 6 == field_type) {
      ret = reader.readI64(meta.data_page_offset_);
    } else if (11 == field_id && 6 == field_type) {
      ret = reader.readI64(meta.dictionary_page_offset_);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_column_chunk(FsCompactReader &reader, FsPqColumnMeta &meta)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (2 == field_id && 6 == field_type) {
      int64_t file_offset = 0;
      ret = reader.readI64(file_offset);
    } else if (3 == field_id && 12 == field_type) {
      ret = fs_pq_parse_column_meta(reader, meta);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_row_group(FsCompactReader &reader, FsPqRowGroup &group)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 9 == field_type) {
      unsigned char elem_type = 0;
      uint32_t count = 0;
      if (OB_FAIL(reader.readListHeader(elem_type, count))) {
      } else if (count > 0 && 12 != elem_type) {
        ret = OB_INVALID_DATA;
      } else {
        for (uint32_t i = 0; i < count && OB_SUCC(ret); ++i) {
          FsPqColumnMeta meta;
          if (OB_FAIL(fs_pq_parse_column_chunk(reader, meta))) {
          } else {
            group.columns_.push_back(meta);
          }
        }
      }
    } else if (3 == field_id && 6 == field_type) {
      ret = reader.readI64(group.num_rows_);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_file_meta(FsCompactReader &reader, FsPqFileMeta &meta)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      int32_t version = 0;
      ret = reader.readI32(version);
    } else if (2 == field_id && 9 == field_type) {
      unsigned char elem_type = 0;
      uint32_t count = 0;
      if (OB_FAIL(reader.readListHeader(elem_type, count))) {
      } else if (count > 0 && 12 != elem_type) {
        ret = OB_INVALID_DATA;
      } else {
        for (uint32_t i = 0; i < count && OB_SUCC(ret); ++i) {
          FsPqSchemaElement element;
          if (OB_FAIL(fs_pq_parse_schema_element(reader, element))) {
          } else {
            meta.schema_.push_back(element);
          }
        }
      }
    } else if (3 == field_id && 6 == field_type) {
      ret = reader.readI64(meta.num_rows_);
    } else if (4 == field_id && 9 == field_type) {
      unsigned char elem_type = 0;
      uint32_t count = 0;
      if (OB_FAIL(reader.readListHeader(elem_type, count))) {
      } else if (count > 0 && 12 != elem_type) {
        ret = OB_INVALID_DATA;
      } else {
        for (uint32_t i = 0; i < count && OB_SUCC(ret); ++i) {
          FsPqRowGroup group;
          if (OB_FAIL(fs_pq_parse_row_group(reader, group))) {
          } else {
            meta.row_groups_.push_back(group);
          }
        }
      }
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

struct FsPqPageHeader
{
  int32_t type_;
  int32_t uncompressed_size_;
  int32_t compressed_size_;
  int32_t num_values_;
  int32_t encoding_;
  int32_t num_nulls_;
  int32_t def_levels_byte_length_;
  int32_t rep_levels_byte_length_;
  bool is_compressed_;
  int32_t dictionary_num_values_;

  FsPqPageHeader()
      : type_(-1), uncompressed_size_(0), compressed_size_(0), num_values_(0),
        encoding_(0), num_nulls_(0), def_levels_byte_length_(0),
        rep_levels_byte_length_(0), is_compressed_(true), dictionary_num_values_(0)
  {
  }
};

static int fs_pq_parse_data_page_header(FsCompactReader &reader, FsPqPageHeader &header)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      ret = reader.readI32(header.num_values_);
    } else if (2 == field_id && 5 == field_type) {
      ret = reader.readI32(header.encoding_);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_data_page_header_v2(FsCompactReader &reader, FsPqPageHeader &header)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      ret = reader.readI32(header.num_values_);
    } else if (2 == field_id && 5 == field_type) {
      ret = reader.readI32(header.num_nulls_);
    } else if (3 == field_id && 5 == field_type) {
      int32_t num_rows = 0;
      ret = reader.readI32(num_rows);
    } else if (4 == field_id && 5 == field_type) {
      ret = reader.readI32(header.encoding_);
    } else if (5 == field_id && 5 == field_type) {
      ret = reader.readI32(header.def_levels_byte_length_);
    } else if (6 == field_id && 5 == field_type) {
      ret = reader.readI32(header.rep_levels_byte_length_);
    } else if (7 == field_id && (1 == field_type || 2 == field_type)) {
      header.is_compressed_ = (1 == field_type);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_dictionary_page_header(FsCompactReader &reader, FsPqPageHeader &header)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      ret = reader.readI32(header.dictionary_num_values_);
    } else if (2 == field_id && 5 == field_type) {
      ret = reader.readI32(header.encoding_);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static int fs_pq_parse_page_header(FsCompactReader &reader, FsPqPageHeader &header)
{
  int ret = OB_SUCCESS;
  int16_t last_id = 0;
  int16_t field_id = 0;
  unsigned char field_type = 0;
  bool finished = false;
  while (OB_SUCC(ret) && !finished) {
    if (OB_FAIL(reader.readFieldHeader(field_type, field_id, last_id))) {
    } else if (0 == field_type) {
      finished = true;
    } else if (1 == field_id && 5 == field_type) {
      ret = reader.readI32(header.type_);
    } else if (2 == field_id && 5 == field_type) {
      ret = reader.readI32(header.uncompressed_size_);
    } else if (3 == field_id && 5 == field_type) {
      ret = reader.readI32(header.compressed_size_);
    } else if (5 == field_id && 12 == field_type) {
      ret = fs_pq_parse_data_page_header(reader, header);
    } else if (7 == field_id && 12 == field_type) {
      ret = fs_pq_parse_dictionary_page_header(reader, header);
    } else if (8 == field_id && 12 == field_type) {
      ret = fs_pq_parse_data_page_header_v2(reader, header);
    } else {
      ret = reader.skip(field_type, 0);
    }
  }
  return ret;
}

static FsType fs_pq_physical_type(int32_t physical_type)
{
  FsType type = FS_TYPE_UNKNOWN;
  switch (physical_type) {
    case 0: type = FS_TYPE_BOOLEAN; break;
    case 1: case 2: type = FS_TYPE_BIGINT; break;
    case 4: case 5: type = FS_TYPE_DOUBLE; break;
    case 6: case 7: type = FS_TYPE_VARCHAR; break;
    default: break;
  }
  return type;
}

static int fs_pq_decompress_page(const char *data, size_t size, int32_t codec, std::string &out)
{
  int ret = OB_SUCCESS;
  if (0 == codec) {
    out.assign(data, size);
  } else if (1 == codec) {
    ret = fs_snappy_decompress(reinterpret_cast<const unsigned char *>(data), size, out);
  } else {
    ret = OB_NOT_SUPPORTED;
  }
  return ret;
}

static size_t fs_pq_bit_width(int32_t max_level)
{
  size_t width = 0;
  while (max_level > 0 && (max_level >> static_cast<int>(width)) > 0) {
    ++width;
  }
  return width;
}

static int fs_pq_decode_rle(const char *data, size_t size, size_t bit_width,
                            size_t count, std::vector<uint32_t> &values)
{
  int ret = OB_SUCCESS;
  values.clear();
  values.reserve(count);
  size_t pos = 0;
  if (bit_width > 32) {
    ret = OB_INVALID_DATA;
  }
  while (OB_SUCC(ret) && values.size() < count) {
    if (pos >= size) {
      ret = OB_INVALID_DATA;
      break;
    }
    uint64_t header = 0;
    int shift = 0;
    bool finished = false;
    while (OB_SUCC(ret) && !finished) {
      if (pos >= size) {
        ret = OB_INVALID_DATA;
        break;
      }
      unsigned char byte = static_cast<unsigned char>(data[pos++]);
      header |= static_cast<uint64_t>(byte & 0x7f) << shift;
      shift += 7;
      if (0 == (byte & 0x80) || shift >= 64) {
        finished = true;
      }
    }
    if (OB_FAIL(ret)) {
      break;
    }
    if (0 == (header & 1)) {
      size_t run = static_cast<size_t>(header >> 1);
      size_t value_bytes = (bit_width + 7) / 8;
      uint32_t value = 0;
      if (value_bytes > 0) {
        if (pos + value_bytes > size) {
          ret = OB_INVALID_DATA;
          break;
        }
        for (size_t i = 0; i < value_bytes; ++i) {
          value |= static_cast<uint32_t>(static_cast<unsigned char>(data[pos + i]))
                   << (8 * i);
        }
        pos += value_bytes;
      }
      for (size_t i = 0; i < run && values.size() < count; ++i) {
        values.push_back(value);
      }
    } else {
      size_t groups = static_cast<size_t>(header >> 1);
      size_t total_values = groups * 8;
      size_t bytes = groups * bit_width;
      if (pos + bytes > size) {
        ret = OB_INVALID_DATA;
        break;
      }
      const uint32_t mask = (bit_width >= 32) ? 0xffffffffu
                                              : (static_cast<uint32_t>(1u << bit_width) - 1u);
      for (size_t i = 0; i < total_values && values.size() < count; ++i) {
        size_t bit = i * bit_width;
        size_t byte_index = bit >> 3;
        size_t offset = bit & 7;
        size_t need = (offset + bit_width + 7) / 8;
        uint64_t chunk = 0;
        for (size_t k = 0; k < need && byte_index + k < bytes; ++k) {
          chunk |= static_cast<uint64_t>(
                       static_cast<unsigned char>(data[pos + byte_index + k]))
                   << (8 * k);
        }
        values.push_back(static_cast<uint32_t>((chunk >> offset) & mask));
      }
      pos += bytes;
    }
  }
  return ret;
}

static int fs_pq_decode_plain(const char *data, size_t size, int32_t physical_type,
                              size_t count, std::vector<FsCell> &values)
{
  int ret = OB_SUCCESS;
  values.clear();
  values.reserve(count);
  FsType type = fs_pq_physical_type(physical_type);
  size_t pos = 0;
  char buffer[64];
  if (FS_TYPE_UNKNOWN == type) {
    ret = OB_NOT_SUPPORTED;
  }
  for (size_t i = 0; i < count && OB_SUCC(ret); ++i) {
    FsCell cell;
    cell.is_null_ = false;
    cell.type_ = type;
    switch (physical_type) {
      case 0: {
        size_t byte_index = i >> 3;
        if (byte_index >= size) {
          ret = OB_INVALID_DATA;
        } else {
          int bit = (static_cast<int>(data[byte_index]) >> (i & 7)) & 1;
          cell.text_ = bit ? "1" : "0";
        }
        break;
      }
      case 1: {
        if (pos + 4 > size) {
          ret = OB_INVALID_DATA;
        } else {
          int32_t value = static_cast<int32_t>(fs_le32(
              reinterpret_cast<const unsigned char *>(data) + pos));
          pos += 4;
          snprintf(buffer, sizeof(buffer), "%d", value);
          cell.text_ = buffer;
        }
        break;
      }
      case 2: {
        if (pos + 8 > size) {
          ret = OB_INVALID_DATA;
        } else {
          const unsigned char *p =
              reinterpret_cast<const unsigned char *>(data) + pos;
          uint64_t raw = static_cast<uint64_t>(fs_le32(p)) |
                         (static_cast<uint64_t>(fs_le32(p + 4)) << 32);
          int64_t value = static_cast<int64_t>(raw);
          pos += 8;
          snprintf(buffer, sizeof(buffer), "%lld", static_cast<long long>(value));
          cell.text_ = buffer;
        }
        break;
      }
      case 4: {
        if (pos + 4 > size) {
          ret = OB_INVALID_DATA;
        } else {
          float value = 0.0f;
          memcpy(&value, data + pos, sizeof(value));
          pos += 4;
          snprintf(buffer, sizeof(buffer), "%.9g", static_cast<double>(value));
          cell.text_ = buffer;
        }
        break;
      }
      case 5: {
        if (pos + 8 > size) {
          ret = OB_INVALID_DATA;
        } else {
          double value = 0.0;
          memcpy(&value, data + pos, sizeof(value));
          pos += 8;
          snprintf(buffer, sizeof(buffer), "%.17g", value);
          cell.text_ = buffer;
        }
        break;
      }
      case 6:
      case 7: {
        if (pos + 4 > size) {
          ret = OB_INVALID_DATA;
        } else {
          uint32_t length = fs_le32(
              reinterpret_cast<const unsigned char *>(data) + pos);
          pos += 4;
          if (pos + length > size) {
            ret = OB_INVALID_DATA;
          } else {
            cell.text_.assign(data + pos, length);
            pos += length;
          }
        }
        break;
      }
      default:
        ret = OB_NOT_SUPPORTED;
        break;
    }
    if (OB_SUCC(ret)) {
      values.push_back(cell);
    }
  }
  return ret;
}

static int fs_pq_read_column(const std::string &file_data,
                             const FsPqColumnMeta &meta,
                             int32_t max_def_level,
                             size_t row_count,
                             std::vector<FsCell> &column)
{
  int ret = OB_SUCCESS;
  column.clear();
  column.resize(row_count);
  FsType cell_type = fs_pq_physical_type(meta.type_);
  if (FS_TYPE_UNKNOWN == cell_type) {
    ret = OB_NOT_SUPPORTED;
  } else if (meta.num_values_ < 0 || meta.data_page_offset_ < 0 ||
             static_cast<size_t>(meta.data_page_offset_) >= file_data.size()) {
    ret = OB_INVALID_DATA;
  }
  size_t offset = (OB_SUCC(ret) && meta.data_page_offset_ >= 0)
                      ? static_cast<size_t>(meta.data_page_offset_)
                      : 0;
  if (OB_SUCC(ret) && meta.dictionary_page_offset_ > 0 &&
      static_cast<size_t>(meta.dictionary_page_offset_) < offset) {
    offset = static_cast<size_t>(meta.dictionary_page_offset_);
  }
  std::vector<FsCell> dictionary;
  size_t row_index = 0;
  int64_t values_seen = 0;
  while (OB_SUCC(ret) && row_index < row_count && values_seen < meta.num_values_) {
    if (offset >= file_data.size()) {
      ret = OB_INVALID_DATA;
      break;
    }
    const unsigned char *base =
        reinterpret_cast<const unsigned char *>(file_data.data()) + offset;
    FsCompactReader reader(base, file_data.size() - offset);
    FsPqPageHeader header;
    if (OB_FAIL(fs_pq_parse_page_header(reader, header))) {
      break;
    }
    size_t payload = offset + reader.offset();
    if (header.compressed_size_ < 0 ||
        payload + static_cast<size_t>(header.compressed_size_) > file_data.size()) {
      ret = OB_INVALID_DATA;
      break;
    }
    const char *payload_data = file_data.data() + payload;
    if (2 == header.type_) {
      std::string page;
      if (OB_FAIL(fs_pq_decompress_page(payload_data, static_cast<size_t>(header.compressed_size_),
                                        meta.codec_, page))) {
      } else if (header.dictionary_num_values_ <= 0) {
        ret = OB_INVALID_DATA;
      } else {
        ret = fs_pq_decode_plain(page.data(), page.size(), meta.type_,
                                 static_cast<size_t>(header.dictionary_num_values_), dictionary);
      }
    } else if (0 == header.type_ || 3 == header.type_) {
      if (header.num_values_ <= 0) {
        ret = OB_INVALID_DATA;
      } else {
        const size_t value_count = static_cast<size_t>(header.num_values_);
        std::vector<uint32_t> def_levels;
        std::string values_data;
        if (3 == header.type_) {
          if (header.rep_levels_byte_length_ < 0 || header.def_levels_byte_length_ < 0 ||
              header.rep_levels_byte_length_ + header.def_levels_byte_length_ >
                  header.compressed_size_) {
            ret = OB_INVALID_DATA;
          } else {
            size_t pos = static_cast<size_t>(header.rep_levels_byte_length_);
            if (max_def_level > 0) {
              ret = fs_pq_decode_rle(payload_data + pos,
                                     static_cast<size_t>(header.def_levels_byte_length_),
                                     fs_pq_bit_width(max_def_level), value_count, def_levels);
            } else {
              def_levels.assign(value_count, 0);
            }
            pos += static_cast<size_t>(header.def_levels_byte_length_);
            if (OB_SUCC(ret)) {
              size_t values_size = static_cast<size_t>(header.compressed_size_) - pos;
              if (header.is_compressed_) {
                ret = fs_pq_decompress_page(payload_data + pos, values_size, meta.codec_,
                                            values_data);
              } else {
                values_data.assign(payload_data + pos, values_size);
              }
            }
          }
        } else {
          std::string page;
          if (OB_FAIL(fs_pq_decompress_page(payload_data,
                                            static_cast<size_t>(header.compressed_size_),
                                            meta.codec_, page))) {
          } else {
            size_t pos = 0;
            if (max_def_level > 0) {
              if (pos + 4 > page.size()) {
                ret = OB_INVALID_DATA;
              } else {
                uint32_t length = fs_le32(
                    reinterpret_cast<const unsigned char *>(page.data()) + pos);
                pos += 4;
                if (pos + length > page.size()) {
                  ret = OB_INVALID_DATA;
                } else {
                  ret = fs_pq_decode_rle(page.data() + pos, length,
                                         fs_pq_bit_width(max_def_level), value_count,
                                         def_levels);
                  pos += length;
                }
              }
            } else {
              def_levels.assign(value_count, 0);
            }
            if (OB_SUCC(ret)) {
              values_data.assign(page.data() + pos, page.size() - pos);
            }
          }
        }
        if (OB_SUCC(ret)) {
          size_t non_null = 0;
          for (size_t i = 0; i < value_count; ++i) {
            uint32_t level = (i < def_levels.size()) ? def_levels[i] : 0;
            if (static_cast<int32_t>(level) >= max_def_level) {
              ++non_null;
            }
          }
          std::vector<FsCell> values;
          if (0 == header.encoding_) {
            ret = fs_pq_decode_plain(values_data.data(), values_data.size(), meta.type_,
                                     non_null, values);
          } else if (2 == header.encoding_ || 8 == header.encoding_) {
            if (dictionary.empty() || values_data.empty()) {
              ret = OB_INVALID_DATA;
            } else {
              size_t bit_width = static_cast<unsigned char>(values_data[0]);
              std::vector<uint32_t> indexes;
              ret = fs_pq_decode_rle(values_data.data() + 1, values_data.size() - 1,
                                     bit_width, non_null, indexes);
              for (size_t i = 0; i < indexes.size() && OB_SUCC(ret); ++i) {
                if (indexes[i] >= dictionary.size()) {
                  ret = OB_INVALID_DATA;
                } else {
                  values.push_back(dictionary[indexes[i]]);
                }
              }
            }
          } else {
            ret = OB_NOT_SUPPORTED;
          }
          if (OB_SUCC(ret)) {
            size_t value_index = 0;
            for (size_t i = 0; i < value_count && row_index < row_count; ++i) {
              uint32_t level = (i < def_levels.size()) ? def_levels[i] : 0;
              if (static_cast<int32_t>(level) >= max_def_level) {
                if (value_index < values.size()) {
                  column[row_index] = values[value_index];
                }
                ++value_index;
              }
              column[row_index].type_ = cell_type;
              ++row_index;
            }
            values_seen += static_cast<int64_t>(value_count);
          }
        }
      }
    }
    offset = payload + static_cast<size_t>(header.compressed_size_);
  }
  return ret;
}

static int fs_build_parquet_table(const std::string &path, FsTable &table)
{
  int ret = OB_SUCCESS;
  std::string data;
  if (OB_FAIL(fs_read_file(path, data))) {
  } else if (data.size() < 12 || 0 != memcmp(data.data(), "PAR1", 4) ||
             0 != memcmp(data.data() + data.size() - 4, "PAR1", 4)) {
    ret = OB_INVALID_DATA;
  } else {
    const uint32_t meta_length =
        fs_le32(reinterpret_cast<const unsigned char *>(data.data()) + data.size() - 8);
    if (static_cast<size_t>(meta_length) + 8 > data.size()) {
      ret = OB_INVALID_DATA;
    } else {
      FsPqFileMeta meta;
      const unsigned char *meta_data =
          reinterpret_cast<const unsigned char *>(data.data()) + data.size() - 8 - meta_length;
      FsCompactReader reader(meta_data, meta_length);
      if (OB_FAIL(fs_pq_parse_file_meta(reader, meta))) {
      } else if (meta.schema_.size() <= 1) {
        ret = OB_INVALID_DATA;
      } else {
        std::vector<FsPqSchemaElement> leaves;
        for (size_t i = 1; i < meta.schema_.size() && OB_SUCC(ret); ++i) {
          if (meta.schema_[i].num_children_ > 0 || 2 == meta.schema_[i].repetition_) {
            ret = OB_NOT_SUPPORTED;
          } else {
            leaves.push_back(meta.schema_[i]);
          }
        }
        for (size_t c = 0; c < leaves.size() && OB_SUCC(ret); ++c) {
          FsColumn column;
          column.name_ = leaves[c].name_;
          column.type_ = fs_pq_physical_type(leaves[c].type_);
          column.nullable_ = (1 == leaves[c].repetition_);
          if (FS_TYPE_UNKNOWN == column.type_) {
            ret = OB_NOT_SUPPORTED;
          } else {
            table.columns_.push_back(column);
          }
        }
        int64_t total_rows = 0;
        for (size_t i = 0; i < meta.row_groups_.size() && OB_SUCC(ret); ++i) {
          if (meta.row_groups_[i].num_rows_ < 0 ||
              meta.row_groups_[i].num_rows_ > static_cast<int64_t>(FS_MAX_ROWS)) {
            ret = OB_SIZE_OVERFLOW;
          } else {
            total_rows += meta.row_groups_[i].num_rows_;
          }
        }
        if (OB_SUCC(ret) && total_rows > static_cast<int64_t>(FS_MAX_ROWS)) {
          ret = OB_SIZE_OVERFLOW;
        }
        std::vector<std::vector<FsCell> > columns;
        columns.resize(leaves.size());
        for (size_t rg = 0; rg < meta.row_groups_.size() && OB_SUCC(ret); ++rg) {
          const FsPqRowGroup &group = meta.row_groups_[rg];
          if (group.columns_.size() != leaves.size()) {
            ret = OB_INVALID_DATA;
            break;
          }
          for (size_t c = 0; c < leaves.size() && OB_SUCC(ret); ++c) {
            std::vector<FsCell> values;
            int32_t max_def_level = (1 == leaves[c].repetition_) ? 1 : 0;
            if (OB_FAIL(fs_pq_read_column(data, group.columns_[c], max_def_level,
                                          static_cast<size_t>(group.num_rows_), values))) {
            } else {
              columns[c].insert(columns[c].end(), values.begin(), values.end());
            }
          }
        }
        if (OB_SUCC(ret)) {
          size_t row_count = 0;
          for (size_t c = 0; c < columns.size(); ++c) {
            row_count = std::max(row_count, columns[c].size());
          }
          table.rows_.resize(row_count);
          for (size_t r = 0; r < row_count; ++r) {
            table.rows_[r].resize(columns.size());
            for (size_t c = 0; c < columns.size(); ++c) {
              if (r < columns[c].size()) {
                table.rows_[r][c] = columns[c][r];
              }
            }
          }
        }
      }
    }
  }
  return ret;
}

static int fs_build_glob_table(const std::string &pattern, FsTable &table)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(fs_check_path(pattern))) {
  } else {
    size_t slash = pattern.rfind('/');
    std::string dir;
    std::string mask;
    if (std::string::npos == slash) {
      dir = ".";
      mask = pattern;
    } else {
      dir = pattern.substr(0, slash);
      if (dir.empty()) {
        dir = "/";
      }
      mask = pattern.substr(slash + 1);
    }
    FsColumn column;
    column.name_ = "file";
    column.type_ = FS_TYPE_VARCHAR;
    column.nullable_ = false;
    table.columns_.push_back(column);
    DIR *dirp = opendir(dir.c_str());
    if (NULL == dirp) {
      ret = OB_FILE_NOT_EXIST;
    } else {
      struct dirent *entry = NULL;
      while (OB_SUCC(ret) && NULL != (entry = readdir(dirp))) {
        std::string name = entry->d_name;
        if ("." == name || ".." == name) {
          continue;
        }
        if (!fs_match_mask(mask, 0, name, 0)) {
          continue;
        }
        std::string full = (dir == "/") ? ("/" + name) : (dir + "/" + name);
        struct stat st;
        if (0 != stat(full.c_str(), &st) || !S_ISREG(st.st_mode)) {
          continue;
        }
        if (table.rows_.size() >= FS_MAX_ROWS) {
          ret = OB_SIZE_OVERFLOW;
          break;
        }
        std::vector<FsCell> row;
        row.resize(1);
        row[0].is_null_ = false;
        row[0].text_ = full;
        row[0].type_ = FS_TYPE_VARCHAR;
        table.rows_.push_back(row);
      }
      closedir(dirp);
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// SQL generation
// ---------------------------------------------------------------------------
static std::string fs_table_to_select(const FsTable &table)
{
  std::string sql;
  sql.reserve(64 + table.rows_.size() * 64);
  if (table.rows_.empty()) {
    sql = "(select ";
    for (size_t c = 0; c < table.columns_.size(); ++c) {
      if (c > 0) {
        sql += ", ";
      }
      sql += "NULL as " + fs_backquote(table.columns_[c].name_);
    }
    sql += " limit 0)";
  } else {
    for (size_t r = 0; r < table.rows_.size(); ++r) {
      if (0 == r) {
        sql += "(select ";
      } else {
        sql += " union all select ";
      }
      for (size_t c = 0; c < table.columns_.size(); ++c) {
        if (c > 0) {
          sql += ", ";
        }
        const FsCell &cell = table.rows_[r][c];
        sql += fs_cell_literal(cell, table.columns_[c].type_);
        sql += " as " + fs_backquote(table.columns_[c].name_);
      }
    }
    sql += ")";
  }
  return sql;
}

static std::string fs_table_to_describe(const FsTable &table)
{
  std::string sql;
  for (size_t c = 0; c < table.columns_.size(); ++c) {
    if (0 == c) {
      sql += "select ";
    } else {
      sql += " union all select ";
    }
    const FsColumn &column = table.columns_[c];
    sql += fs_quote_string(column.name_);
    sql += " as `column_name`, ";
    sql += fs_quote_string(fs_type_name(column.type_));
    sql += " as `column_type`, ";
    sql += std::string("'") + (column.nullable_ ? "YES" : "NO") + "' as `null`, ";
    sql += "cast(NULL as char) as `key`, ";
    sql += "cast(NULL as char) as `default`, ";
    sql += "cast(NULL as char) as `extra`";
  }
  return sql;
}

// ---------------------------------------------------------------------------
// Function call parsing
// ---------------------------------------------------------------------------
static void fs_split_top_level(const std::string &sql, size_t begin, size_t end,
                               std::vector<std::string> &parts)
{
  parts.clear();
  size_t part_begin = begin;
  int depth = 0;
  size_t pos = begin;
  while (pos < end) {
    char c = sql[pos];
    if ('\'' == c || '"' == c || '`' == c) {
      char quote = c;
      ++pos;
      while (pos < end) {
        char q = sql[pos];
        if ('\\' == q && '`' != quote && pos + 1 < end) {
          pos += 2;
        } else if (q == quote) {
          if (pos + 1 < end && sql[pos + 1] == quote) {
            pos += 2;
          } else {
            ++pos;
            break;
          }
        } else {
          ++pos;
        }
      }
      continue;
    } else if ('(' == c) {
      ++depth;
    } else if (')' == c) {
      --depth;
    } else if (',' == c && 0 == depth) {
      parts.push_back(sql.substr(part_begin, pos - part_begin));
      part_begin = pos + 1;
    }
    ++pos;
  }
  parts.push_back(sql.substr(part_begin, end - part_begin));
}

static bool fs_parse_int_token(const std::string &token, long long &value)
{
  bool result = false;
  std::string s = fs_trim(token);
  size_t pos = 0;
  bool negative = false;
  if (pos < s.size() && ('+' == s[pos] || '-' == s[pos])) {
    negative = ('-' == s[pos]);
    ++pos;
  }
  if (pos < s.size()) {
    long long v = 0;
    size_t digits = 0;
    while (pos < s.size() && fs_is_digit(s[pos])) {
      v = v * 10 + (s[pos] - '0');
      ++pos;
      ++digits;
    }
    if (digits > 0 && pos == s.size()) {
      result = true;
      value = negative ? -v : v;
    }
  }
  return result;
}

static int fs_parse_call(const std::string &sql, size_t open_pos, size_t close_pos,
                         FsCallOptions &options)
{
  int ret = OB_SUCCESS;
  std::vector<std::string> parts;
  fs_split_top_level(sql, open_pos + 1, close_pos, parts);
  bool first_positional = true;
  for (size_t i = 0; i < parts.size() && OB_SUCC(ret) && options.syntax_valid_; ++i) {
    std::string part = fs_trim(parts[i]);
    if (part.empty()) {
      continue;
    }
    if ('\'' == part[0]) {
      if (!first_positional) {
        options.syntax_valid_ = false;
        break;
      }
      size_t value_pos = parts[i].find('\'');
      std::string value;
      if (!fs_parse_sql_string(parts[i], value_pos, value) ||
          value_pos != parts[i].size()) {
        options.syntax_valid_ = false;
        break;
      }
      options.path_ = value;
      options.has_path_ = true;
      first_positional = false;
    } else {
      size_t pos = 0;
      while (pos < part.size() && fs_is_ident_char(part[pos])) {
        ++pos;
      }
      std::string name = fs_lower(part.substr(0, pos));
      size_t after_name = fs_skip_spaces(part, pos);
      if (pos == 0 || after_name >= part.size() || '=' != part[after_name]) {
        options.syntax_valid_ = false;
        break;
      }
      size_t value_pos = fs_skip_spaces(part, after_name + 1);
      std::string value;
      if (value_pos < part.size() && '\'' == part[value_pos]) {
        size_t string_pos = value_pos;
        if (!fs_parse_sql_string(part, string_pos, value) || string_pos != part.size()) {
          options.syntax_valid_ = false;
          break;
        }
        if ("format" == name) {
          options.format_ = fs_lower(value);
          options.has_format_ = true;
        }
      } else {
        std::string token = fs_trim(part.substr(value_pos));
        long long int_value = 0;
        if (fs_parse_int_token(token, int_value)) {
          if ("sample_size" == name) {
            options.sample_size_ = int_value;
            options.has_sample_size_ = true;
          }
        } else {
          std::string lower = fs_lower(token);
          if ("header" == name) {
            options.header_ = ("true" == lower || "1" == lower || "on" == lower);
            options.has_header_ = true;
          } else if ("format" == name) {
            options.format_ = lower;
            options.has_format_ = true;
          }
        }
      }
    }
  }
  return ret;
}

static void fs_copy_quoted(const std::string &sql, size_t &pos, std::string &out)
{
  size_t begin = pos;
  char quote = sql[pos];
  ++pos;
  while (pos < sql.size()) {
    char c = sql[pos];
    if ('\\' == c && '`' != quote && pos + 1 < sql.size()) {
      pos += 2;
    } else if (c == quote) {
      if (pos + 1 < sql.size() && quote == sql[pos + 1]) {
        pos += 2;
      } else {
        ++pos;
        break;
      }
    } else {
      ++pos;
    }
  }
  out.assign(sql, begin, pos - begin);
}

static bool fs_has_alias_ahead(const std::string &sql, size_t pos)
{
  bool result = false;
  if (pos < sql.size()) {
    if ('`' == sql[pos]) {
      result = true;
    } else if (fs_is_ident_start(sql[pos])) {
      size_t begin = pos;
      size_t end = pos;
      while (end < sql.size() && fs_is_ident_char(sql[end])) {
        ++end;
      }
      std::string word = fs_lower(sql.substr(begin, end - begin));
      static const char *keywords[] = {
        "where", "group", "order", "having", "limit", "join", "inner", "left",
        "right", "full", "cross", "natural", "on", "using", "union", "except",
        "intersect", "window", "offset", "fetch", "for", "partition",
        "straight_join", "with", "qualify", "lateral", "tablesample", "into",
        "from", "select", "values", "use", "force", "ignore"
      };
      result = true;
      for (size_t i = 0; i < sizeof(keywords) / sizeof(keywords[0]); ++i) {
        if (fs_ieq(word, keywords[i])) {
          result = false;
          break;
        }
      }
    }
  }
  return result;
}

static bool fs_match_enable_var(const std::string &sql, size_t pos, size_t &end)
{
  static const char *patterns[] = {
    "@@session.enable_file_sql",
    "@@local.enable_file_sql",
    "@@enable_file_sql"
  };
  bool matched = false;
  for (size_t i = 0; i < sizeof(patterns) / sizeof(patterns[0]) && !matched; ++i) {
    size_t len = strlen(patterns[i]);
    if (pos + len <= sql.size()) {
      matched = true;
      for (size_t k = 0; k < len; ++k) {
        if (tolower(static_cast<unsigned char>(sql[pos + k])) !=
            tolower(static_cast<unsigned char>(patterns[i][k]))) {
          matched = false;
          break;
        }
      }
      if (matched && pos + len < sql.size() && fs_is_ident_char(sql[pos + len])) {
        matched = false;
      }
      if (matched) {
        end = pos + len;
      }
    }
  }
  return matched;
}

static int fs_rewrite_generic(const std::string &in, bool enabled, std::string &out,
                              bool &has_file_sql, bool &changed)
{
  int ret = OB_SUCCESS;
  std::string result;
  result.reserve(in.size() * 2);
  size_t pos = 0;
  size_t generated_alias = 0;
  bool invalid_syntax = false;
  while (pos < in.size() && OB_SUCC(ret) && !invalid_syntax) {
    char c = in[pos];
    if ('\'' == c || '"' == c || '`' == c) {
      size_t begin = pos;
      size_t cursor = pos;
      std::string dummy;
      fs_copy_quoted(in, cursor, dummy);
      result.append(in, begin, cursor - begin);
      pos = cursor;
      continue;
    } else if ('-' == c && pos + 1 < in.size() && '-' == in[pos + 1]) {
      size_t begin = pos;
      while (pos < in.size() && '\n' != in[pos]) {
        ++pos;
      }
      result.append(in, begin, pos - begin);
      continue;
    } else if ('/' == c && pos + 1 < in.size() && '*' == in[pos + 1]) {
      size_t begin = pos;
      pos += 2;
      while (pos + 1 < in.size() && !('/' == in[pos] && '*' == in[pos + 1])) {
        ++pos;
      }
      pos = (pos + 1 < in.size()) ? pos + 2 : in.size();
      result.append(in, begin, pos - begin);
      continue;
    } else if ('@' == c && pos + 1 < in.size() && '@' == in[pos + 1]) {
      size_t end = 0;
      if (fs_match_enable_var(in, pos, end)) {
        result += "ifnull(@enable_file_sql, 0)";
        pos = end;
        changed = true;
        continue;
      }
      result.push_back(c);
      ++pos;
      continue;
    } else if (fs_is_ident_start(c)) {
      size_t ident_end = pos;
      while (ident_end < in.size() && fs_is_ident_char(in[ident_end])) {
        ++ident_end;
      }
      std::string ident = fs_lower(in.substr(pos, ident_end - pos));
      size_t after_ident = fs_skip_spaces(in, ident_end);
      bool is_file_func = ("read_csv" == ident || "read_json" == ident ||
                           "read_parquet" == ident || "glob" == ident);
      if (is_file_func && after_ident < in.size() && '(' == in[after_ident]) {
        has_file_sql = true;
        size_t close_pos = 0;
        if (!fs_find_matching_paren(in, after_ident, close_pos)) {
          invalid_syntax = true;
          break;
        }
        FsCallOptions options;
        if (OB_FAIL(fs_parse_call(in, after_ident, close_pos, options))) {
        } else if (!options.syntax_valid_ || !options.has_path_) {
          invalid_syntax = true;
          break;
        } else if (!enabled) {
          ret = OB_NOT_SUPPORTED;
          break;
        } else if (options.has_sample_size_ && -1 != options.sample_size_) {
          ret = OB_NOT_SUPPORTED;
          break;
        } else if ("read_json" == ident && options.has_format_ &&
                   ("newline_delimited" != options.format_ &&
                    "ndjson" != options.format_ && "auto" != options.format_)) {
          ret = OB_NOT_SUPPORTED;
          break;
        } else {
          FsTable table;
          if ("read_csv" == ident) {
            bool header = options.has_header_ ? options.header_ : true;
            ret = fs_build_csv_table(options.path_, header, table);
          } else if ("read_json" == ident) {
            ret = fs_build_json_table(options.path_, table);
          } else if ("read_parquet" == ident) {
            ret = fs_build_parquet_table(options.path_, table);
          } else {
            ret = fs_build_glob_table(options.path_, table);
          }
          if (OB_SUCC(ret)) {
            result += fs_table_to_select(table);
            size_t alias_pos = fs_skip_spaces(in, close_pos + 1);
            if (!fs_has_alias_ahead(in, alias_pos)) {
              result += " as " + fs_backquote("__ob_file_sql_t" + std::to_string(generated_alias++));
            }
            changed = true;
            pos = close_pos + 1;
            continue;
          }
        }
      }
      result.append(in, pos, ident_end - pos);
      pos = ident_end;
      continue;
    } else {
      result.push_back(c);
      ++pos;
    }
  }
  if (OB_SUCC(ret) && !invalid_syntax) {
    out = result;
  }
  return ret;
}

static int fs_build_file_table(const std::string &ident, const FsCallOptions &options,
                               FsTable &table)
{
  int ret = OB_SUCCESS;
  if ("read_csv" == ident) {
    bool header = options.has_header_ ? options.header_ : true;
    ret = fs_build_csv_table(options.path_, header, table);
  } else if ("read_json" == ident) {
    ret = fs_build_json_table(options.path_, table);
  } else if ("read_parquet" == ident) {
    ret = fs_build_parquet_table(options.path_, table);
  } else if ("glob" == ident) {
    ret = fs_build_glob_table(options.path_, table);
  } else {
    ret = OB_NOT_SUPPORTED;
  }
  return ret;
}

static int fs_rewrite_set_variable(const std::string &stmt, std::string &output, bool &changed)
{
  int ret = OB_SUCCESS;
  changed = false;
  size_t pos = 0;
  if (!fs_match_word(stmt, 0, "set", pos)) {
    return ret;
  }
  pos = fs_skip_spaces(stmt, pos);
  size_t scope_end = 0;
  if (fs_match_word(stmt, pos, "session", scope_end) ||
      fs_match_word(stmt, pos, "local", scope_end)) {
    pos = fs_skip_spaces(stmt, scope_end);
  }
  size_t var_end = 0;
  if (fs_match_word(stmt, pos, "enable_file_sql", var_end)) {
    pos = fs_skip_spaces(stmt, var_end);
    if (pos < stmt.size() && '=' == stmt[pos]) {
      pos = fs_skip_spaces(stmt, pos + 1);
      size_t value_end = pos;
      while (value_end < stmt.size() && fs_is_ident_char(stmt[value_end])) {
        ++value_end;
      }
      std::string value = fs_lower(stmt.substr(pos, value_end - pos));
      if ("on" == value || "true" == value || "1" == value || "off" == value ||
          "false" == value || "0" == value) {
        bool on = ("on" == value || "true" == value || "1" == value);
        output = "set @enable_file_sql = ";
        output += on ? "1" : "0";
        size_t rest = fs_skip_spaces(stmt, value_end);
        if (rest < stmt.size() && ';' == stmt[rest]) {
          output += ";";
        }
        changed = true;
      }
    }
  }
  return ret;
}

static bool fs_extract_column_name(const std::string &item_in, std::string &name)
{
  bool result = false;
  std::string item = fs_trim(item_in);
  if (!item.empty()) {
    size_t end = item.size();
    size_t begin = end;
    if ('`' == item[end - 1]) {
      size_t quote_pos = item.rfind('`', end - 2);
      if (std::string::npos != quote_pos) {
        name = item.substr(quote_pos + 1, end - quote_pos - 2);
        begin = quote_pos;
        result = true;
      }
    } else if (fs_is_ident_char(item[end - 1])) {
      while (begin > 0 && fs_is_ident_char(item[begin - 1])) {
        --begin;
      }
      std::string token = item.substr(begin, end - begin);
      if (!token.empty() && !fs_is_digit(token[0])) {
        name = token;
        result = true;
      }
    }
    if (result) {
      std::string prefix = fs_trim(item.substr(0, begin));
      if (!prefix.empty() && '.' != prefix[prefix.size() - 1]) {
        std::string lower_prefix = fs_lower(prefix);
        size_t length = lower_prefix.size();
        bool has_as = false;
        if (length >= 2 && 0 == lower_prefix.compare(length - 2, 2, "as") &&
            (2 == length || fs_is_space(lower_prefix[length - 3]) ||
             ')' == lower_prefix[length - 3] || '\'' == lower_prefix[length - 3] ||
             '`' == lower_prefix[length - 3])) {
          has_as = true;
        }
        if (!has_as) {
          result = false;
        }
      }
    }
  }
  return result;
}

static bool fs_extract_select_columns(const std::string &select_sql,
                                      std::vector<std::string> &names)
{
  bool result = false;
  names.clear();
  size_t pos = 0;
  if (fs_match_word(select_sql, 0, "select", pos)) {
    pos = fs_skip_spaces(select_sql, pos);
    size_t from_pos = select_sql.size();
    int depth = 0;
    size_t scan = pos;
    while (scan < select_sql.size()) {
      char c = select_sql[scan];
      if ('\'' == c || '"' == c || '`' == c) {
        char quote = c;
        ++scan;
        while (scan < select_sql.size()) {
          char q = select_sql[scan];
          if ('\\' == q && '`' != quote && scan + 1 < select_sql.size()) {
            scan += 2;
          } else if (q == quote) {
            if (scan + 1 < select_sql.size() && quote == select_sql[scan + 1]) {
              scan += 2;
            } else {
              ++scan;
              break;
            }
          } else {
            ++scan;
          }
        }
        continue;
      } else if ('(' == c) {
        ++depth;
      } else if (')' == c) {
        --depth;
      } else if (0 == depth) {
        size_t word_end = 0;
        if (fs_match_word(select_sql, scan, "from", word_end)) {
          from_pos = scan;
          break;
        }
      }
      ++scan;
    }
    std::vector<std::string> parts;
    fs_split_top_level(select_sql, pos, from_pos, parts);
    result = true;
    for (size_t i = 0; i < parts.size() && result; ++i) {
      std::string name;
      if (!fs_extract_column_name(parts[i], name)) {
        result = false;
      } else {
        names.push_back(name);
      }
    }
  }
  return result;
}

static int fs_rewrite_copy(const std::string &stmt, bool enabled, std::string &output,
                           bool &contains_file_sql, bool &changed)
{
  int ret = OB_SUCCESS;
  changed = false;
  size_t pos = 0;
  if (!fs_match_word(stmt, 0, "copy", pos)) {
    return ret;
  }
  pos = fs_skip_spaces(stmt, pos);
  if (pos >= stmt.size() || '(' != stmt[pos]) {
    return ret;
  }
  size_t close_pos = 0;
  if (!fs_find_matching_paren(stmt, pos, close_pos)) {
    return ret;
  }
  std::string source = stmt.substr(pos + 1, close_pos - pos - 1);
  size_t after_close = fs_skip_spaces(stmt, close_pos + 1);
  size_t to_end = 0;
  if (!fs_match_word(stmt, after_close, "to", to_end)) {
    return ret;
  }
  size_t path_pos = fs_skip_spaces(stmt, to_end);
  std::string path;
  if (path_pos >= stmt.size() || '\'' != stmt[path_pos] ||
      !fs_parse_sql_string(stmt, path_pos, path)) {
    return ret;
  }
  bool has_header = false;
  size_t opt_pos = fs_skip_spaces(stmt, path_pos);
  if (opt_pos < stmt.size() && '(' == stmt[opt_pos]) {
    size_t opt_close = 0;
    if (!fs_find_matching_paren(stmt, opt_pos, opt_close)) {
      return ret;
    }
    std::vector<std::string> parts;
    fs_split_top_level(stmt, opt_pos + 1, opt_close, parts);
    for (size_t i = 0; i < parts.size() && OB_SUCC(ret); ++i) {
      std::string part = fs_trim(parts[i]);
      std::string lower = fs_lower(part);
      if ("header" == lower || "header = true" == lower) {
        has_header = true;
      } else if (lower.size() >= 6 && 0 == lower.compare(0, 6, "header")) {
        size_t equal_pos = lower.find('=');
        if (std::string::npos != equal_pos) {
          std::string value = fs_trim(lower.substr(equal_pos + 1));
          has_header = ("true" == value || "1" == value || "on" == value);
        }
      } else if (lower.size() >= 6 && 0 == lower.compare(0, 6, "format")) {
        size_t equal_pos = lower.find('=');
        std::string value =
            fs_trim(lower.substr(std::string::npos == equal_pos ? 6 : equal_pos + 1));
        if (value.empty() || 'c' != value[0]) {
          ret = OB_NOT_SUPPORTED;
        }
      }
    }
    if (OB_SUCC(ret)) {
      path_pos = opt_close + 1;
    }
  }
  if (OB_SUCC(ret)) {
    contains_file_sql = true;
    std::string source_rewritten;
    bool source_changed = false;
    bool source_has_file = false;
    std::vector<std::string> names;
    if (OB_FAIL(fs_rewrite_generic(source, enabled, source_rewritten, source_has_file,
                                   source_changed))) {
    } else if (!fs_extract_select_columns(source, names) || names.empty()) {
      ret = OB_NOT_SUPPORTED;
    } else {
      std::string sql;
      sql = "select ";
      for (size_t i = 0; i < names.size(); ++i) {
        if (i > 0) {
          sql += ", ";
        }
        sql += fs_backquote(names[i]);
      }
      sql += " from (";
      if (has_header) {
        sql += "select 0 as `__ob_fs_ord`";
        for (size_t i = 0; i < names.size(); ++i) {
          sql += ", ";
          sql += fs_quote_string(names[i]);
          sql += " as ";
          sql += fs_backquote(names[i]);
        }
        sql += " union all ";
      }
      sql += "select ";
      sql += has_header ? "1" : "0";
      sql += " as `__ob_fs_ord`";
      for (size_t i = 0; i < names.size(); ++i) {
        sql += ", ";
        sql += fs_backquote(names[i]);
      }
      sql += " from (";
      sql += source_changed ? source_rewritten : source;
      sql += ") `__ob_fs_src`";
      sql += ") `__ob_fs_copy` order by `__ob_fs_ord` into outfile ";
      sql += fs_quote_string(path);
      sql += " fields terminated by ','";
      size_t rest = fs_skip_spaces(stmt, path_pos);
      if (rest < stmt.size() && ';' == stmt[rest]) {
        sql += ";";
      }
      output = sql;
      changed = true;
    }
  }
  return ret;
}

static int fs_rewrite_describe(const std::string &stmt, bool enabled, std::string &output,
                               bool &contains_file_sql, bool &changed)
{
  int ret = OB_SUCCESS;
  changed = false;
  size_t pos = 0;
  bool matched = fs_match_word(stmt, 0, "describe", pos);
  if (!matched) {
    matched = fs_match_word(stmt, 0, "desc", pos);
  }
  if (!matched) {
    return ret;
  }
  pos = fs_skip_spaces(stmt, pos);
  size_t select_end = 0;
  if (!fs_match_word(stmt, pos, "select", select_end)) {
    return ret;
  }
  pos = fs_skip_spaces(stmt, select_end);
  if (pos >= stmt.size() || '*' != stmt[pos]) {
    return ret;
  }
  pos = fs_skip_spaces(stmt, pos + 1);
  size_t from_end = 0;
  if (!fs_match_word(stmt, pos, "from", from_end)) {
    return ret;
  }
  pos = fs_skip_spaces(stmt, from_end);
  size_t ident_end = pos;
  while (ident_end < stmt.size() && fs_is_ident_char(stmt[ident_end])) {
    ++ident_end;
  }
  std::string ident = fs_lower(stmt.substr(pos, ident_end - pos));
  size_t open_pos = fs_skip_spaces(stmt, ident_end);
  if (("read_csv" != ident && "read_json" != ident && "read_parquet" != ident &&
       "glob" != ident) || open_pos >= stmt.size() || '(' != stmt[open_pos]) {
    return ret;
  }
  contains_file_sql = true;
  size_t close_pos = 0;
  if (!fs_find_matching_paren(stmt, open_pos, close_pos)) {
    return ret;
  }
  FsCallOptions options;
  if (OB_FAIL(fs_parse_call(stmt, open_pos, close_pos, options))) {
  } else if (!options.syntax_valid_ || !options.has_path_) {
    contains_file_sql = false;
  } else if (!enabled) {
    ret = OB_NOT_SUPPORTED;
  } else if (options.has_sample_size_ && -1 != options.sample_size_) {
    ret = OB_NOT_SUPPORTED;
  } else if ("read_json" == ident && options.has_format_ &&
             ("newline_delimited" != options.format_ && "ndjson" != options.format_ &&
              "auto" != options.format_)) {
    ret = OB_NOT_SUPPORTED;
  } else {
    FsTable table;
    if (OB_FAIL(fs_build_file_table(ident, options, table))) {
    } else {
      output = fs_table_to_describe(table);
      size_t rest = fs_skip_spaces(stmt, close_pos + 1);
      if (rest < stmt.size() && ';' == stmt[rest]) {
        output += ";";
      }
      changed = true;
    }
  }
  return ret;
}

static int fs_rewrite_statement(const std::string &input, bool enabled, std::string &output,
                                bool &changed, bool &contains_file_sql)
{
  int ret = OB_SUCCESS;
  changed = false;
  contains_file_sql = false;
  std::string stmt = fs_trim(input);
  if (!stmt.empty()) {
    if (OB_FAIL(fs_rewrite_set_variable(stmt, output, changed))) {
    } else if (!changed &&
               OB_FAIL(fs_rewrite_copy(stmt, enabled, output, contains_file_sql, changed))) {
    } else if (!changed &&
               OB_FAIL(fs_rewrite_describe(stmt, enabled, output, contains_file_sql, changed))) {
    } else if (!changed) {
      ret = fs_rewrite_generic(input, enabled, output, contains_file_sql, changed);
    }
  }
  return ret;
}

} // namespace file_sql_detail

int ob_rewrite_file_sql(common::ObIAllocator &alloc,
                        const ObSQLSessionInfo &session,
                        const common::ObString &sql,
                        common::ObString &rewritten,
                        bool &changed,
                        bool &contains_file_sql)
{
  int ret = OB_SUCCESS;
  changed = false;
  contains_file_sql = false;
  std::string input;
  if (!sql.empty()) {
    input.assign(sql.ptr(), sql.length());
  }
  bool enabled = false;
  const common::ObObj *var_value =
      session.get_user_variable_value(common::ObString::make_string(FS_ENABLE_VAR_NAME));
  if (NULL != var_value && !var_value->is_null() &&
      var_value->is_integer_type() && 0 != var_value->get_int()) {
    enabled = true;
  }
  std::string output;
  if (OB_SUCC(ret)) {
    ret = file_sql_detail::fs_rewrite_statement(input, enabled, output,
                                               changed, contains_file_sql);
  }
  if (OB_SUCC(ret) && changed) {
    char *buffer = NULL;
    if (output.empty()) {
      rewritten = common::ObString::make_empty_string();
    } else if (NULL == (buffer = static_cast<char *>(alloc.alloc(output.size())))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      MEMCPY(buffer, output.data(), output.size());
      rewritten = common::ObString(static_cast<int32_t>(output.size()), buffer);
    }
  } else if (OB_SUCC(ret)) {
    rewritten = sql;
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
